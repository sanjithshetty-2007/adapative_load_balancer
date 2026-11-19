#!/usr/bin/env python3
"""
Terminal 2 of run_demo.sh: builds the same network as

  sudo mn --custom topology/topology.py --topo adaptive --link tc --mac \
          --switch ovsk --controller remote,ip=127.0.0.1,port=6633

then types the demo commands at the mininet> prompt one by one (each runs
through Mininet's real CLI, exactly as if typed), checks what the
controller detected, and leaves you at the normal mininet> prompt.
Run as root (run_demo.sh does this inside tmux).
"""

import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                       os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)
os.chdir(REPO)

from mininet.cli import CLI                              # noqa: E402
from mininet.link import TCLink                          # noqa: E402
from mininet.log import setLogLevel                      # noqa: E402
from mininet.net import Mininet                          # noqa: E402
from mininet.node import OVSSwitch, RemoteController     # noqa: E402

from topology.topology import AdaptiveLoadBalancerTopo   # noqa: E402

TMP = os.environ.get("DEMO_TMP", "/tmp")          # changed only for testing
EVENTS = os.path.join(TMP, "demo_events.jsonl")
CLIENT_LOG = os.path.join(TMP, "demo_client.log")
CLIENT_SECONDS = 90
SPEED = float(os.environ.get("DEMO_SPEED", "1"))   # >1 only for testing

GRN, YEL, RED, B, RST = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"


def wait(seconds):
    time.sleep(seconds / SPEED)


def note(text):
    print(YEL + "# " + text + RST, flush=True)


def run(net, line, after=1.5):
    """Show the command at a mininet> prompt and run it through the real CLI."""
    print(B + "mininet> " + RST + line, flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".mn", delete=False) as f:
        f.write(line + "\n")
        path = f.name
    setLogLevel("output")
    CLI(net, script=path)
    setLogLevel("info")
    os.remove(path)
    wait(after)


def events():
    out = []
    if os.path.exists(EVENTS):
        for line in open(EVENTS):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def wait_until(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False


def server_cmd(name):
    return ("%s python3 app/backend_server.py --name=%s --report-to 10.0.0.254:9000 "
            "> /tmp/demo_%s.log 2>&1 &" % (name, name, name))


def main():

    setLogLevel("info")

    print("Waiting for the controller (terminal 1) ...", flush=True)
    if not wait_until(lambda: ":6633 " in subprocess.run(
            "ss -ltn", shell=True, capture_output=True, text=True).stdout, 30):
        print(RED + "POX is not listening on port 6633; see terminal 1." + RST)

    print(B + "$ sudo mn --custom topology/topology.py --topo adaptive --link tc --mac "
          "--switch ovsk --controller remote,ip=127.0.0.1,port=6633" + RST, flush=True)
    net = Mininet(topo=AdaptiveLoadBalancerTopo(), switch=OVSSwitch, link=TCLink,
                  controller=None, autoSetMacs=True, build=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6633)
    net.build()
    net.start()
    wait(2)

    # ---------------------------------------------------- network + servers
    note("The network: 3 servers h1-h3, 4 clients h4-h7, switch s1, ctl = path to the controller")
    run(net, "net", 2)
    note("Start the three TCP servers; they report their load to the controller every 0.5 s")
    for h in ("h1", "h2", "h3"):
        run(net, server_cmd(h), 0.5)
    ok = wait_until(lambda: {"h1", "h2", "h3"} <= {
        e.get("server") for e in events() if e.get("ev") == "server_up"}, 20)
    if not ok:
        print(RED + "Not all servers reported to the controller yet." + RST, flush=True)
    wait(2)

    note("Every host can reach every other through the SDN switch")
    run(net, "pingall", 2)
    note("Clients use the virtual IP 10.0.0.100; the controller picks a real server")
    for _ in range(3):
        run(net, "h4 curl -s 10.0.0.100:8080", 1)
    note("The controller answered ARP for the virtual IP with a made-up MAC")
    run(net, "h4 ip -c=never neigh show 10.0.0.100", 2)
    note("Rules the controller installed: mod_nw_dst (VIP -> server), mod_nw_src (server -> VIP)")
    run(net, "sh ovs-ofctl dump-flows s1 | grep mod_nw", 3)

    # ---------------------------------------------------- live run + failures
    open(os.path.join(TMP, "demo_ready"), "w").close()
    note("Terminal 3 now starts the client (16 threads, %d s). Watch terminals 1 and 3." % CLIENT_SECONDS)
    wait_until(lambda: os.path.exists(os.path.join(TMP, "demo_client_started")), 60)
    t0 = time.time()

    def at(second):
        time.sleep(max(0.0, t0 + second / SPEED - time.time()))

    at(12)
    note("Overload h1: heavy traffic sent straight to it for 15 s (terminal 1: h1 OVERLOADED)")
    run(net, "h7 python3 app/client.py --vip 10.0.0.1 --threads 8 --work-ms 400 "
             "--duration 15 > /tmp/demo_hog.log 2>&1 &", 0)
    at(34)
    note("Crash h2's server (terminal 1: h2 SERVER_DOWN, FLOW RULES DELETED; terminal 3: h2 goes to 0)")
    run(net, 'h2 pkill -f "backend_server.py --name=h2"', 0)
    at(46)
    note("Restart h2 (terminal 1: h2 SERVER_UP)")
    run(net, server_cmd("h2"), 0)
    at(56)
    note("Take h3's link down (terminal 1: h3 SERVER_DOWN, switch port down)")
    run(net, "link s1 h3 down", 0)
    at(68)
    note("Bring h3's link back up (terminal 1: h3 SERVER_UP)")
    run(net, "link s1 h3 up", 0)

    note("Waiting for the client to finish ...")
    wait_until(lambda: os.path.exists(os.path.join(TMP, "demo_client_done")), CLIENT_SECONDS + 60)
    wait(2)

    # ---------------------------------------------------- checks
    ev = events()

    def has(name, server, reason=""):
        return [e for e in ev if e.get("ev") == name and e.get("server") == server
                and reason in e.get("reason", "")]

    client = open(CLIENT_LOG).read() if os.path.exists(CLIENT_LOG) else ""
    checks = [
        ("h1, h2, h3 reported their load to the controller",
         all(has("server_up", s) for s in ("h1", "h2", "h3"))),
        ("h1 detected as OVERLOADED", bool(has("overloaded", "h1"))),
        ("h2 crash detected (report connection closed)",
         bool(has("server_down", "h2", "report connection closed"))),
        ("h2 flow rules deleted", bool(has("flows_deleted", "h2"))),
        ("h2 back UP after restart", len(has("server_up", "h2")) >= 2),
        ("h3 link down detected (switch port down)",
         bool(has("server_down", "h3", "switch port down"))),
        ("h3 back UP after link up", len(has("server_up", "h3")) >= 2),
        ("client run finished", "Done in" in client),
    ]
    print()
    print(B + "Checks (from the controller's event log):" + RST)
    for name, passed in checks:
        print("  %s  %s" % (GRN + "PASS" + RST if passed else RED + "FAIL" + RST, name))
    print(B + ("ALL CHECKS PASSED" if all(p for _, p in checks) else "SOME CHECKS FAILED") + RST)
    print()
    note("Demo finished. This is the normal mininet> prompt: type any command, or 'exit' to stop everything.")

    CLI(net)
    net.stop()
    subprocess.run('pkill -f "[a]daptive_lb"', shell=True)
    subprocess.run("tmux kill-session -t lbdemo", shell=True)


if __name__ == "__main__":
    main()
