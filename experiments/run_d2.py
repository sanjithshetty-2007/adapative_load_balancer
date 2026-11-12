#!/usr/bin/env python3
"""
D2 experiments: static round robin vs adaptive SDN load balancing.

Every scenario runs once per mode (rr, adaptive) and is repeated --reps
times. Raw data goes to results/d2/raw/<scenario>/<mode>_r<rep>/.
Finished runs are skipped, so the script can be stopped and restarted.

Run as root in the VM:
  sudo python3 experiments/run_d2.py             # everything, 3 reps
  sudo python3 experiments/run_d2.py --reps 1    # quick
  sudo python3 experiments/run_d2.py --only e5_crash
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from mnlib import Testbed, APP, client_hosts  # noqa: E402

RAW = os.path.join(ROOT, "results", "d2", "raw")
DURATION = 20

EQUAL = [0.05, 0.05, 0.05]

# Times are seconds after the clients start.
SCENARIOS = {
    "e1_low": {
        "title": "Equal servers, low load (8 client threads)",
        "delays": EQUAL, "threads": 8},
    "e1_med": {
        "title": "Equal servers, medium load (16 client threads)",
        "delays": EQUAL, "threads": 16},
    "e1_high": {
        "title": "Equal servers, high load (32 client threads)",
        "delays": EQUAL, "threads": 32},
    "e2_hetero": {
        "title": "Unequal servers: h1 fast (20 ms), h2 50 ms, h3 slow (150 ms)",
        "delays": [0.02, 0.05, 0.15], "threads": 16},
    "e3_overload": {
        "title": "Server overload: extra heavy traffic sent straight to h1 "
                 "from 5 s to 15 s",
        "delays": EQUAL, "threads": 16,
        "hog": {"at": 5, "seconds": 10, "host": "h7", "target": "10.0.0.1",
                "threads": 8, "work_ms": 400}},
    "e4_netload": {
        "title": "Network load: h1's link flooded with bulk traffic "
                 "from 5 s to 15 s",
        "delays": EQUAL, "threads": 16,
        "flood": {"at": 5, "seconds": 10, "src": "h1", "dst": "h7",
                  "dst_ip": "10.0.0.7"}},
    "e5_crash": {
        "title": "Server failure: h2's server process killed at 7 s, "
                 "restarted at 14 s",
        "delays": EQUAL, "threads": 16,
        "crash": {"server": "h2", "at": 7, "restart": 14}},
    "e6_linkdown": {
        "title": "Link failure: h3's link down at 7 s, back up at 14 s",
        "delays": EQUAL, "threads": 16,
        "link": {"server": "h3", "at": 7, "up": 14}},
}

MODES = ["rr", "adaptive"]


def run_one(name, sc, mode, rep, pox_dir):

    out = os.path.join(RAW, name, "%s_r%d" % (mode, rep))
    done_marker = os.path.join(out, "run.json")

    if os.path.exists(done_marker):
        print("skip %s %s r%d (done)" % (name, mode, rep), flush=True)
        return

    print("\n=== %s | %s | rep %d ===" % (name, mode, rep), flush=True)
    actions = []

    with Testbed(out, mode=mode, pox_dir=pox_dir,
                 server_delays=sc["delays"]) as tb:

        hosts = client_hosts()
        per_host = max(1, sc["threads"] // len(hosts))
        t0 = time.time() + 1.5
        clients = []

        for h in hosts:
            csv_path = os.path.join(out, "client_%s.csv" % h)
            cmd = tb.client_cmd(csv_path, threads=per_host,
                                duration=DURATION, start_at=t0, name=h)
            clients.append(tb.popen(h, cmd, "client_%s.log" % h))

        if "flood" in sc:
            f = sc["flood"]
            tb.popen(f["dst"], ["python3", os.path.join(APP, "traffic_gen.py"),
                                "--sink", "--port", "5001"], "sink.log")

        plan = []

        if "hog" in sc:
            g = sc["hog"]
            plan.append((g["at"], "hog_start", g))

        if "flood" in sc:
            plan.append((sc["flood"]["at"], "flood_start", sc["flood"]))

        if "crash" in sc:
            c = sc["crash"]
            plan.append((c["at"], "server_kill", c))
            plan.append((c["restart"], "server_restart", c))

        if "link" in sc:
            lk = sc["link"]
            plan.append((lk["at"], "link_down", lk))
            plan.append((lk["up"], "link_up", lk))

        plan.append((DURATION / 2.0, "dump_flows", None))
        plan.sort(key=lambda p: p[0])

        for at, what, arg in plan:

            time.sleep(max(0.0, t0 + at - time.time()))
            actions.append({"t": round(time.time(), 4), "at": at,
                            "action": what})

            if what == "hog_start":
                cmd = tb.client_cmd(os.path.join(out, "hog.csv"),
                                    threads=arg["threads"],
                                    duration=arg["seconds"],
                                    vip=arg["target"], work_ms=arg["work_ms"],
                                    timeout=5, name="hog")
                tb.popen(arg["host"], cmd, "hog.log")

            elif what == "flood_start":
                tb.popen(arg["src"],
                         ["python3", os.path.join(APP, "traffic_gen.py"),
                          "--send", "%s:5001" % arg["dst_ip"],
                          "--seconds", str(arg["seconds"]),
                          "--streams", "4"], "flood.log")

            elif what == "server_kill":
                tb.kill_server(arg["server"])

            elif what == "server_restart":
                idx = int(arg["server"][1:]) - 1
                tb.start_server(arg["server"], sc["delays"][idx])

            elif what == "link_down":
                tb.net.configLinkStatus("s1", arg["server"], "down")

            elif what == "link_up":
                tb.net.configLinkStatus("s1", arg["server"], "up")

            elif what == "dump_flows":
                tb.dump_flows(os.path.join(out, "flows_mid_run.txt"))

        for p in clients:
            p.wait(DURATION + 60)

        time.sleep(1.0)

    meta = {"scenario": name, "title": sc["title"], "mode": mode,
            "rep": rep, "t0": t0, "duration": DURATION,
            "threads": sc["threads"], "delays": sc["delays"],
            "actions": actions}

    with open(done_marker, "w") as f:
        json.dump(meta, f, indent=2)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--only", default="",
                        help="comma separated scenario names")
    parser.add_argument("--pox", default=None)
    args = parser.parse_args()

    names = [n for n in SCENARIOS
             if not args.only or n in args.only.split(",")]
    os.makedirs(RAW, exist_ok=True)

    with open(os.path.join(RAW, "..", "scenarios.json"), "w") as f:
        json.dump(SCENARIOS, f, indent=2)

    total = len(names) * len(MODES) * args.reps
    n = 0
    started = time.time()

    for rep in range(1, args.reps + 1):
        for name in names:
            for mode in MODES:
                n += 1
                print("[%d/%d] elapsed %.0f s" % (n, total,
                                                  time.time() - started),
                      flush=True)
                try:
                    run_one(name, SCENARIOS[name], mode, rep, args.pox)
                except Exception as error:
                    print("RUN FAILED %s %s r%d: %s"
                          % (name, mode, rep, error), flush=True)

    print("\nAll runs finished in %.0f s" % (time.time() - started))


if __name__ == "__main__":
    main()
