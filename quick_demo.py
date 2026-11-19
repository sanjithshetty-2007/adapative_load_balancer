#!/usr/bin/env python3
"""
Quick automatic demo (about 3-4 minutes, no typing needed).

  sudo python3 quick_demo.py             # runs straight through
  sudo python3 quick_demo.py --step      # waits for Enter before each part
  sudo python3 quick_demo.py --skip-compare

Part 1  Network + SDN controller: topology, ARP for the VIP, curl to the VIP,
        the address-rewrite flow rules in the switch.
Part 2  Live run in adaptive mode with clients on h4-h7, while the script
        overloads h1, crashes and restarts h2, and takes h3's link down and
        up. A live table shows what the controller sees and decides.
Part 3  Static round robin vs adaptive on unequal servers, 15 s each.

Everything is written to results/quick_demo/.
"""

import argparse
import csv
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from mnlib import Testbed, client_hosts, VIP  # noqa: E402

OUT = os.path.join(ROOT, "results", "quick_demo")
TTY = sys.stdout.isatty()

B = "\033[1m" if TTY else ""
DIM = "\033[2m" if TTY else ""
RED = "\033[31m" if TTY else ""
GRN = "\033[32m" if TTY else ""
YEL = "\033[33m" if TTY else ""
CYN = "\033[36m" if TTY else ""
RST = "\033[0m" if TTY else ""

ARGS = None


# ------------------------------------------------------------------ output

def banner(title, who=""):
    print("\n" + CYN + "=" * 72 + RST)
    print(B + " " + title + RST + ("   " + DIM + "(" + who + ")" + RST if who else ""))
    print(CYN + "=" * 72 + RST)


def say(text):
    print("  " + text, flush=True)


def pause(seconds=2.5):
    if ARGS.step:
        try:
            input(DIM + "  ... press Enter to continue" + RST)
        except EOFError:
            pass
    else:
        time.sleep(seconds)


class EventTail:
    """Reads new lines of the controller's event log."""

    def __init__(self, path):
        self.path = path
        self.pos = 0

    def new(self):
        out = []
        if not os.path.exists(self.path):
            return out
        with open(self.path) as f:
            f.seek(self.pos)
            for line in f:
                if not line.endswith("\n"):
                    break
                self.pos += len(line.encode())
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


def read_rows(paths):
    rows = []
    for p in paths:
        if os.path.exists(p):
            with open(p, newline="") as f:
                rows += list(csv.DictReader(f))
    return rows


def stats(rows, seconds):
    ok = [r for r in rows if r["ok"] == "1"]
    lat = sorted(float(r["latency_ms"]) for r in ok)
    per = {}
    for r in ok:
        per[r["server"]] = per.get(r["server"], 0) + 1

    def pct(p):
        return lat[min(len(lat) - 1, int(round(p / 100.0 * (len(lat) - 1))))] if lat else 0.0

    return {"requests": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
            "retried": sum(1 for r in rows if r["attempts"] != "1"),
            "avg": sum(lat) / len(lat) if lat else 0.0, "p95": pct(95),
            "rps": len(ok) / float(seconds), "per": per}


def start_clients(tb, out, threads_per_host, duration):
    t0 = time.time() + 1.0
    paths, procs = [], []
    for h in client_hosts():
        path = os.path.join(out, "client_%s.csv" % h)
        cmd = tb.client_cmd(path, threads=threads_per_host, duration=duration,
                            start_at=t0, name=h)
        procs.append(tb.popen(h, cmd, "client_%s.log" % h))
        paths.append(path)
    return t0, paths, procs


# ------------------------------------------------------------------ part 1

def part1(tb):

    banner("PART 1  Network and SDN controller", "Sanjith: Mininet + POX")
    net = tb.net
    say("Mininet built one Open vSwitch (s1) and these hosts:")
    for h in net.hosts:
        role = ("TCP server" if h.name in ("h1", "h2", "h3") else
                "client" if h.name in client_hosts() else "path to the controller")
        say("   %-4s %-11s %s" % (h.name, h.IP(), role))
    say("Server links are 10 Mbit/s, client links 100 Mbit/s. POX controls s1 over OpenFlow.")
    say("The 3 servers are running and report their load to the controller every 0.5 s.")
    pause()

    say("")
    say(B + "pingall" + RST + " - every host can reach every other host through the switch:")
    loss = net.pingAll(timeout="1")
    say("   packet loss: %s%%  %s" % (loss, GRN + "OK" + RST if loss == 0 else RED + "check" + RST))
    pause(1.5)

    h4 = net.get("h4")
    say("")
    say(B + "curl to the virtual IP %s:8080" % VIP + RST + " from client h4, three times:")
    for i in range(3):
        out = h4.cmd("curl -s -m 5 http://%s:8080/" % VIP).strip().splitlines()
        say("   request %d -> %s" % (i + 1, out[0] if out else RED + "no reply" + RST))
    say("No host owns %s. The controller picks a server for each new connection." % VIP)
    pause(1.5)

    say("")
    say(B + "ARP" + RST + " - h4 learned this MAC for the virtual IP (the controller answered):")
    neigh = h4.cmd("ip -c=never neigh show %s" % VIP).strip()
    say("   " + (neigh or "no entry"))
    pause(1.5)

    say("")
    say(B + "Flow rules" + RST + " the controller installed in s1 for those connections:")
    flows = tb.dump_flows(os.path.join(tb.out_dir, "flows_part1.txt"))
    shown = 0
    for line in flows.splitlines():
        if "mod_nw" not in line:
            continue
        line = re.sub(r"(cookie|duration|table|n_packets|n_bytes|idle_age|hard_age)=[^,]*,\s*", "", line.strip())
        say("   " + line[:150])
        shown += 1
        if shown == 4:
            break
    say("Forward rule: client -> VIP is rewritten to the server (mod_nw_dst).")
    say("Reverse rule: server -> client is rewritten back to the VIP (mod_nw_src).")
    pause(3)


# ------------------------------------------------------------------ part 2

TIMELINE = [
    (0, None, "Normal load: new connections are spread over h1, h2, h3"),
    (6, "hog", "Overload h1 with heavy traffic sent straight to it (10 s)"),
    (18, "kill", "Crash h2's server process"),
    (25, "restart", "Restart h2's server"),
    (29, "link_down", "Take h3's link down"),
    (35, "link_up", "Bring h3's link back up"),
]
LIVE_SECONDS = 42
NOTABLE = ("server_up", "server_down", "overloaded", "normal", "flows_deleted")


def draw(t, step_idx, state, rates, log_lines):

    lines = []
    if TTY:
        lines.append("\033[2J\033[H")
    lines.append(B + "PART 2  Live run, adaptive mode" + RST + DIM +
                 "   (Sammed's scenarios, Sanjith's reactions, Sharvesh's decisions)" + RST)
    lines.append("16 client threads on h4-h7 -> VIP %s     t = %2d s / %d s" % (VIP, t, LIVE_SECONDS))
    lines.append("")
    lines.append(B + "%-4s %-7s %-12s %6s %6s %7s %6s %11s" % (
        "srv", "health", "load", "active", "queue", "avg ms", "link", "new conn/s") + RST)
    for s in state or []:
        health = (GRN + "UP     " + RST) if s["up"] else (RED + "DOWN   " + RST)
        load = (YEL + "OVERLOADED  " + RST) if s["overloaded"] else "ok          "
        lines.append("%-4s %s %s %6s %6s %7.0f %5.0f%% %11.1f" % (
            s["name"], health, load, s["active"], s["queued"], s["avg_ms"],
            100 * s["util"], rates.get(s["name"], 0.0)))
    if not state:
        lines.append("(waiting for the controller...)")
    lines.append("")
    lines.append(B + "Timeline" + RST)
    for i, (at, _, text) in enumerate(TIMELINE):
        mark = (GRN + "[x]" + RST) if i < step_idx else (YEL + "[>]" + RST) if i == step_idx else "[ ]"
        lines.append("  %s %2d s  %s" % (mark, at, text))
    lines.append("")
    lines.append(B + "What the controller detected" + RST)
    for l in log_lines[-9:]:
        lines.append("  " + l)
    if not TTY:
        lines.append("-" * 72)
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def part2(tb):

    banner("PART 2  Live run with overload, crash and link failure",
           "Sammed + Sanjith + Sharvesh")
    say("Clients on h4-h7 now send requests to the VIP for %d s." % LIVE_SECONDS)
    say("The script will break things on purpose; watch the table react:")
    for at, _, text in TIMELINE[1:]:
        say("   at %2d s: %s" % (at, text))
    pause(4)

    out = tb.out_dir
    tail = EventTail(tb.events_path)
    tail.new()                       # skip everything logged so far
    t0, paths, procs = start_clients(tb, out, 4, LIVE_SECONDS)

    state, assigns, log_lines = None, [], []
    step_idx = 0
    done_actions = set()

    while True:

        now = time.time()
        t = now - t0
        if t >= LIVE_SECONDS + 1:
            break

        for i, (at, action, _) in enumerate(TIMELINE):
            if t >= at and i not in done_actions:
                done_actions.add(i)
                step_idx = i
                if action == "hog":
                    cmd = tb.client_cmd(os.path.join(out, "hog.csv"), threads=8,
                                        duration=10, vip="10.0.0.1", work_ms=400,
                                        timeout=5, name="hog")
                    tb.popen("h7", cmd, "hog.log")
                elif action == "kill":
                    tb.kill_server("h2")
                elif action == "restart":
                    tb.start_server("h2", 0.05)
                elif action == "link_down":
                    tb.net.configLinkStatus("s1", "h3", "down")
                elif action == "link_up":
                    tb.net.configLinkStatus("s1", "h3", "up")
                if action:
                    log_lines.append(DIM + "%+5.1fs  >> %s" % (t, TIMELINE[i][2]) + RST)

        for e in tail.new():
            ev = e.get("ev")
            if ev == "state":
                state = e["servers"]
            elif ev == "assign":
                assigns.append((e["t"], e["server"]))
            elif ev in NOTABLE:
                rel = e["t"] - t0
                if ev == "server_down":
                    txt = RED + "%s DOWN" % e["server"] + RST + "        " + e.get("reason", "")
                elif ev == "server_up":
                    txt = GRN + "%s UP" % e["server"] + RST + "          " + e.get("reason", "")
                elif ev == "overloaded":
                    txt = YEL + "%s OVERLOADED" % e["server"] + RST + "  " + e.get("reason", "")
                elif ev == "normal":
                    txt = GRN + "%s normal" % e["server"] + RST + "      " + e.get("reason", "")
                else:
                    txt = "%s flow rules deleted (%s connections re-placed)" % (
                        e["server"], e.get("connections", 0))
                if rel > -1:
                    log_lines.append("%+5.1fs  %s" % (rel, txt))

        assigns = [a for a in assigns if now - a[0] < 3.0]
        rates = {}
        for _, name in assigns:
            rates[name] = rates.get(name, 0) + 1 / 3.0

        draw(max(0, int(t)), step_idx, state, rates, log_lines)
        time.sleep(1.0)

    for p in procs:
        p.wait(30)

    s = stats(read_rows(paths), LIVE_SECONDS)
    print()
    say(B + "Result of the live run" + RST)
    say("requests %d, successful %d, failed %d, needed a retry %d" % (
        s["requests"], s["ok"], s["failed"], s["retried"]))
    say("average %.0f ms, p95 %.0f ms, throughput %.0f req/s" % (s["avg"], s["p95"], s["rps"]))
    say("served by: " + ", ".join("%s %d" % kv for kv in sorted(s["per"].items())))
    say("While h1 was overloaded and h2 / h3 were down, the controller sent new")
    say("connections to the healthy servers and deleted the rules of the failed ones.")
    pause(4)


# ------------------------------------------------------------------ part 3

def run_mode(mode, seconds):
    out = os.path.join(OUT, "compare_" + mode)
    with Testbed(out, mode, pox_dir=ARGS.pox, server_delays=(0.02, 0.05, 0.15)) as tb:
        say("   %-20s running for %d s ..." % (
            "static round robin" if mode == "rr" else "adaptive (SDN)", seconds))
        _, paths, procs = start_clients(tb, out, 4, seconds)
        for p in procs:
            p.wait(seconds + 30)
    return stats(read_rows(paths), seconds)


def part3():

    banner("PART 3  Static round robin vs adaptive", "Sharvesh: evaluation")
    say("Unequal servers: h1 fast (20 ms), h2 medium (50 ms), h3 slow (150 ms).")
    say("Same clients (16 threads) for 15 s, once per mode:")
    res = {m: run_mode(m, 15) for m in ("rr", "adaptive")}
    print()
    say(B + "%-20s %8s %8s %8s %7s   %s" % ("mode", "req/s", "avg ms", "p95 ms", "failed",
                                          "h1 / h2 / h3") + RST)
    for m, label in (("rr", "static round robin"), ("adaptive", "adaptive (SDN)")):
        r = res[m]
        say("%-20s %8.0f %8.0f %8.0f %7d   %d / %d / %d" % (
            label, r["rps"], r["avg"], r["p95"], r["failed"],
            r["per"].get("h1", 0), r["per"].get("h2", 0), r["per"].get("h3", 0)))
    rr, ad = res["rr"], res["adaptive"]
    if rr["rps"] and rr["p95"]:
        say("")
        change = 100 * (1 - ad["p95"] / rr["p95"])
        say(GRN + "Adaptive: %.1fx the throughput of round robin, p95 latency %.0f%% %s." % (
            ad["rps"] / rr["rps"], abs(change), "lower" if change >= 0 else "higher") + RST)
    say("Round robin sends a third of the work to the slow server; adaptive sends")
    say("most of it to the fast one, based on the servers' load reports.")


# ------------------------------------------------------------------ main

def main():

    global ARGS
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--step", action="store_true", help="wait for Enter between parts")
    parser.add_argument("--skip-compare", action="store_true", help="skip part 3")
    parser.add_argument("--pox", default=None, help="POX folder (default ~/pox)")
    ARGS = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 quick_demo.py")

    started = time.time()
    banner("Adaptive Load Balancer with SDN - quick demo")
    say("CN mini project #20: Sanjith, Sammed, Sharvesh")
    say("Starting the POX controller, the Mininet network and 3 TCP servers ...")

    try:
        with Testbed(os.path.join(OUT, "live"), "adaptive", pox_dir=ARGS.pox) as tb:
            part1(tb)
            part2(tb)
        if not ARGS.skip_compare:
            part3()
    except RuntimeError as error:
        print(RED + "\nCould not start: %s" % error + RST)
        print("Logs are in %s" % OUT)
        sys.exit(1)

    banner("Demo finished in %.0f s" % (time.time() - started))
    say("Controller event logs and client data: %s" % OUT)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run 'sudo mn -c' if anything is left over.")
