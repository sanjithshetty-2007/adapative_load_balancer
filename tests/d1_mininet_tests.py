#!/usr/bin/env python3
"""
D1 tests: end-to-end checks in Mininet with the POX controller.

Run as root in the VM:
  sudo python3 tests/d1_mininet_tests.py [--pox ~/pox]

Results go to results/d1/:
  d1_test_report.md      PASS/FAIL table with evidence
  d1_results.json        the same, machine readable
  pingall.txt, flows_during_load.txt, controller_events.jsonl, CSVs, logs
"""

import argparse
import csv
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from mnlib import (Testbed, SERVERS, VIP, client_hosts,  # noqa: E402
                   read_events)

OUT = os.path.join(ROOT, "results", "d1")
VIP_MAC = "00:00:00:00:01:00"

results = []


def record(test_id, name, passed, evidence):

    results.append({"id": test_id, "test": name,
                    "status": "PASS" if passed else "FAIL",
                    "evidence": evidence})
    print("[%s] %s %s - %s" % ("PASS" if passed else "FAIL", test_id, name,
                                evidence), flush=True)


def read_csv(path):

    if not os.path.exists(path):
        return []

    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def safe(test_id, name, fn):

    try:
        fn()
    except Exception as error:
        record(test_id, name, False, "error: %s" % error)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--pox", default=None)
    args = parser.parse_args()

    os.makedirs(OUT, exist_ok=True)

    for f in os.listdir(OUT):
        if f.endswith((".csv", ".json", ".log", ".txt", ".jsonl", ".md")):
            os.remove(os.path.join(OUT, f))

    started = time.time()

    with Testbed(OUT, mode="rr", pox_dir=args.pox) as tb:

        net = tb.net
        h4 = net.get("h4")

        # T1 connectivity through the SDN switch
        def t1():
            loss = net.pingAll(timeout="1")
            with open(os.path.join(OUT, "pingall.txt"), "w") as f:
                f.write("pingAll packet loss: %s%%\n" % loss)
            record("T1", "All hosts reach each other through s1 (pingall)",
                   loss == 0, "packet loss %s%%" % loss)
        safe("T1", "pingall", t1)

        # T2 HTTP request to the VIP (works with curl, like the old demo)
        def t2():
            out = h4.cmd("curl -s -m 5 http://%s:8080/" % VIP)
            with open(os.path.join(OUT, "curl_vip.txt"), "w") as f:
                f.write(out)
            record("T2", "curl to the virtual IP gets a server reply",
                   "Served by:" in out,
                   out.strip().splitlines()[0] if out.strip() else "no reply")
        safe("T2", "curl VIP", t2)

        # T3 the controller answered ARP for the VIP
        def t3():
            neigh = h4.cmd("ip -c=never neigh show %s" % VIP).strip()
            record("T3", "Controller answers ARP for the VIP",
                   VIP_MAC in neigh.lower(), neigh or "no ARP entry")
        safe("T3", "VIP ARP", t3)

        # T4 round robin spreads sequential requests evenly
        def t4():
            path = os.path.join(OUT, "t4_round_robin.csv")
            h4.cmd(" ".join(tb.client_cmd(path, threads=1, requests=30,
                                          name="h4")))
            rows = read_csv(path)
            counts = {}
            for r in rows:
                if r["ok"] == "1":
                    counts[r["server"]] = counts.get(r["server"], 0) + 1
            ok = (len(rows) == 30 and len(counts) == 3
                  and max(counts.values()) - min(counts.values()) <= 1)
            record("T4", "Round robin spreads 30 requests over 3 servers",
                   ok, "per server: %s" % dict(sorted(counts.items())))
        safe("T4", "round robin", t4)

        # T5 concurrent clients on 4 hosts, flow rules captured during load
        def t5():
            procs = []
            start_at = time.time() + 1.0
            for name in client_hosts():
                path = os.path.join(OUT, "t5_concurrent_%s.csv" % name)
                cmd = tb.client_cmd(path, threads=8, requests=10,
                                    start_at=start_at, name=name)
                procs.append(tb.popen(name, cmd, "client_%s.log" % name))
            time.sleep(2.0)
            flows = tb.dump_flows(os.path.join(OUT,
                                               "flows_during_load.txt"))
            for p in procs:
                p.wait(120)
            rows = []
            for name in client_hosts():
                rows += read_csv(os.path.join(
                    OUT, "t5_concurrent_%s.csv" % name))
            ok_rows = [r for r in rows if r["ok"] == "1"]
            servers = sorted({r["server"] for r in ok_rows})
            record("T5", "4 client hosts x 8 threads x 10 requests at once",
                   len(rows) == 320 and len(ok_rows) == 320,
                   "%d/%d succeeded, served by %s"
                   % (len(ok_rows), len(rows), servers))

            # T6 the rewrite rules exist in the switch
            fwd = flows.count("mod_nw_dst:10.0.0.")
            rev = flows.count("mod_nw_src:%s" % VIP)
            record("T6", "Switch holds forward and reverse rewrite rules",
                   fwd > 0 and rev > 0,
                   "%d forward (VIP->server) and %d reverse (server->VIP) "
                   "rules seen" % (fwd, rev))

            # T7 clients only ever talk to the VIP
            peers = sorted({r["peer"] for r in ok_rows})
            record("T7", "Clients only see the VIP, never a server IP",
                   peers == [VIP], "peer addresses seen: %s" % peers)
        safe("T5", "concurrent clients", t5)

        time.sleep(1.5)
        events = tb.events()

        # T8 socket -> SDN integration: load reports reach the controller
        def t8():
            per = {}
            for e in events:
                if e["ev"] == "load":
                    per[e["server"]] = per.get(e["server"], 0) + 1
            names = [n for n, _ in SERVERS]
            ok = all(per.get(n, 0) >= 5 for n in names)
            record("T8", "Servers send load reports to the controller "
                   "over TCP", ok, "LOAD messages received: %s"
                   % dict(sorted(per.items())))
        safe("T8", "load reports", t8)

        # T9 the controller placed every connection itself
        def t9():
            assigns = [e for e in events if e["ev"] == "assign"]
            per = {}
            for e in assigns:
                per[e["server"]] = per.get(e["server"], 0) + 1
            record("T9", "Controller placed each new TCP connection "
                   "(PacketIn -> FlowMod)", len(assigns) >= 350,
                   "%d connections placed: %s"
                   % (len(assigns), dict(sorted(per.items()))))
        safe("T9", "controller decisions", t9)

        # T10 network load monitoring is running
        def t10():
            stats = [e for e in events if e["ev"] == "port_stats"]
            last = stats[-1]["ports"] if stats else {}
            record("T10", "Controller polls switch port statistics",
                   len(stats) >= 3 and len(last) == 3,
                   "%d samples, last: %s" % (len(stats), last))
        safe("T10", "port stats", t10)

    write_report(time.time() - started)


def write_report(seconds):

    passed = sum(1 for r in results if r["status"] == "PASS")

    with open(os.path.join(OUT, "d1_results.json"), "w") as f:
        json.dump({"passed": passed, "total": len(results),
                   "seconds": round(seconds, 1), "tests": results}, f,
                  indent=2)

    lines = [
        "# D1 test report (generated)",
        "",
        "Run in Mininet with the POX controller in round robin mode "
        "(`tests/d1_mininet_tests.py`).",
        "",
        "**Result: %d of %d tests passed** (%.0f s)." % (
            passed, len(results), seconds),
        "",
        "| # | Test | Result | Evidence |",
        "|---|---|---|---|",
    ]

    for r in results:
        lines.append("| %s | %s | %s | %s |" % (
            r["id"], r["test"], r["status"],
            r["evidence"].replace("|", "/")))

    lines += ["", "Evidence files in this folder: `pingall.txt`, "
              "`curl_vip.txt`, `flows_during_load.txt`, "
              "`controller_events.jsonl`, `t4_*.csv`, `t5_*.csv`, "
              "`server_*.log`, `pox.log`.", ""]

    with open(os.path.join(OUT, "d1_test_report.md"), "w") as f:
        f.write("\n".join(lines))

    print("\nD1: %d/%d passed" % (passed, len(results)))


if __name__ == "__main__":
    main()
