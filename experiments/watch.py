#!/usr/bin/env python3
"""
Live view of the controller for demos: server health, load, link use and
the latest events, refreshed every second from the controller's event log.

  python3 experiments/watch.py [events.jsonl]

Default log: results/demo/controller_events.jsonl (written by demo.py).
"""

import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PATH = (sys.argv[1] if len(sys.argv) > 1 else
        os.path.join(ROOT, "results", "demo", "controller_events.jsonl"))
NOTABLE = {"server_up", "server_down", "overloaded", "normal",
           "flows_deleted", "switch_up"}


def main():

    state, mode, notes, assigns = None, "?", [], []
    pos = 0

    while True:

        if os.path.exists(PATH):
            with open(PATH) as f:
                f.seek(pos)
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ev = e.get("ev")
                    if ev == "state":
                        state = e["servers"]
                    elif ev == "assign":
                        mode = e.get("mode", mode)
                        assigns.append((e["t"], e["server"]))
                    elif ev in NOTABLE:
                        notes.append(e)
                pos = f.tell()

        now = time.time()
        assigns = [a for a in assigns if now - a[0] < 5]
        notes = notes[-8:]
        out = ["\033[2J\033[H", "Controller view  (%s)   mode: %s\n" % (PATH, mode),
               "%-4s %-6s %-11s %6s %6s %7s %7s %6s %10s" % (
                   "srv", "health", "load", "active", "queue", "svc_ms",
                   "avg_ms", "link", "new conn/s")]

        for s in state or []:
            rate = sum(1 for _, n in assigns if n == s["name"]) / 5.0
            out.append("%-4s %-6s %-11s %6s %6s %7s %7s %5.0f%% %10.1f" % (
                s["name"], "UP" if s["up"] else "DOWN",
                "OVERLOADED" if s["overloaded"] else "ok",
                s["active"], s["queued"], s["svc_ms"], s["avg_ms"],
                100 * s["util"], rate))

        if state is None:
            out.append("(waiting for the controller...)")

        out.append("\nLatest events:")
        for e in notes:
            out.append("  %s  %-13s %-3s %s" % (
                time.strftime("%H:%M:%S", time.localtime(e.get("t", 0))),
                e["ev"], e.get("server", ""), e.get("reason", "")))

        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
