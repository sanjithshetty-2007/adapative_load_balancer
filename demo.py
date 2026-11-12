#!/usr/bin/env python3
"""
Live demo for the viva: starts POX, the Mininet network and the three
servers, then opens the Mininet CLI.

  sudo python3 demo.py                     # adaptive mode, equal servers
  sudo python3 demo.py --mode rr           # static round robin
  sudo python3 demo.py --unequal           # h1 fast, h2 medium, h3 slow

In a second terminal, watch what the controller sees and decides:
  python3 experiments/watch.py

Type 'exit' in the CLI to stop everything.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from mininet.cli import CLI      # noqa: E402

from mnlib import Testbed        # noqa: E402

HELP = """
==================================================================
 POX controller ({mode}) + Mininet + 3 servers are running.
 Virtual IP: 10.0.0.100:8080   servers h1-h3   clients h4-h7

 Try these in the mininet> prompt:
   h4 curl -s 10.0.0.100:8080
   h4 python3 {app}/client.py --threads 8 --requests 10
   h4 python3 {app}/client.py --threads 16 --duration 15 &
   sh ovs-ofctl dump-flows s1 | grep mod_nw

 Overload h1 (traffic sent straight to h1, bypassing the VIP):
   h7 python3 {app}/client.py --vip 10.0.0.1 --threads 8 --work-ms 400 --duration 15 &

 Flood h1's link (network load):
   h7 python3 {app}/traffic_gen.py --sink &
   h1 python3 {app}/traffic_gen.py --send 10.0.0.7:5001 --seconds 15 --streams 4 &

 Server failure / link failure:
   h2 pkill -f "backend_server.py --name h2"
   link s1 h3 down        (and later: link s1 h3 up)

 Controller events: {events}
 Live view in another terminal:  python3 experiments/watch.py
==================================================================
"""


def main():

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--mode", choices=["adaptive", "rr"],
                        default="adaptive")
    parser.add_argument("--unequal", action="store_true",
                        help="h1 20 ms, h2 50 ms, h3 150 ms per request")
    parser.add_argument("--pox", default=None, help="POX folder (~/pox)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 demo.py")

    out = os.path.join(ROOT, "results", "demo")
    delays = (0.02, 0.05, 0.15) if args.unequal else (0.05, 0.05, 0.05)

    with Testbed(out, args.mode, pox_dir=args.pox,
                 server_delays=delays) as tb:
        print(HELP.format(mode=args.mode, app=os.path.join(ROOT, "app"),
                          events=tb.events_path))
        CLI(tb.net)


if __name__ == "__main__":
    main()
