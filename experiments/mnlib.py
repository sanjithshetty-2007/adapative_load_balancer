#!/usr/bin/env python3
"""
Helpers shared by the D1 tests and D2 experiments (run as root in the VM).

Starts the POX controller, the Mininet network and the servers, and reads
the controller's event log.
"""

import json
import os
import pwd
import signal
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from mininet.clean import cleanup                      # noqa: E402
from mininet.link import Link, TCLink                  # noqa: E402
from mininet.log import setLogLevel                    # noqa: E402
from mininet.net import Mininet                        # noqa: E402
from mininet.node import OVSSwitch, RemoteController   # noqa: E402

from topology.topology import (AdaptiveLoadBalancerTopo, SERVERS,  # noqa: E402
                               CLIENTS, CONTROLLER_HOST, VIP)

APP = os.path.join(ROOT, "app")
CONTROLLER_DIR = os.path.join(ROOT, "controller")
OF_PORT = 6633
REPORT_PORT = 9000
SERVICE_PORT = 8080


def default_pox_dir():

    user = os.environ.get("SUDO_USER")
    home = pwd.getpwnam(user).pw_dir if user else os.path.expanduser("~")
    return os.environ.get("POX_DIR", os.path.join(home, "pox"))


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True).stdout


def full_cleanup():

    sh("pkill -f adaptive_lb >/dev/null 2>&1")
    setLogLevel("error")
    cleanup()
    time.sleep(0.5)


class Testbed:
    """One controller + network + servers, used as a context manager."""

    def __init__(self, out_dir, mode, pox_dir=None, use_bw=True,
                 server_delays=(0.05, 0.05, 0.05), workers=4,
                 overload_ms=250):

        self.out_dir = out_dir
        self.mode = mode
        self.pox_dir = pox_dir or default_pox_dir()
        self.use_bw = use_bw
        self.server_delays = list(server_delays)
        self.workers = workers
        self.overload_ms = overload_ms
        self.events_path = os.path.join(out_dir, "controller_events.jsonl")
        self.pox = None
        self.net = None
        self.servers = {}
        self.extra = []

        os.makedirs(out_dir, exist_ok=True)

    # ------------------------------------------------------------ start/stop

    def __enter__(self):

        try:
            return self._start()
        except BaseException:
            self.stop()
            raise

    def _start(self):

        full_cleanup()

        if os.path.exists(self.events_path):
            os.remove(self.events_path)

        self.start_pox()
        self.start_network()

        if not self.wait_event(lambda e: e["ev"] == "switch_up", 20):
            raise RuntimeError("switch did not connect to POX; see pox.log")

        for i, (name, _) in enumerate(SERVERS):
            self.start_server(name, self.server_delays[i])

        names = {n for n, _ in SERVERS}
        ok = self.wait_for(
            lambda: names <= {e["server"] for e in self.events()
                              if e["ev"] == "server_up"}, 20)

        if not ok:
            raise RuntimeError("servers did not report to the controller; "
                               "see server logs and pox.log")

        # Let L2 rules and ARP settle before measuring.
        time.sleep(1.0)
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start_pox(self):

        pox_py = os.path.join(self.pox_dir, "pox.py")

        if not os.path.exists(pox_py):
            raise RuntimeError("POX not found at %s (set POX_DIR)" % pox_py)

        env = dict(os.environ)
        env["PYTHONPATH"] = CONTROLLER_DIR + os.pathsep + env.get(
            "PYTHONPATH", "")
        servers = ",".join("%s=%s" % s for s in SERVERS)
        cmd = [sys.executable, pox_py, "log.level", "--WARNING",
               "openflow.of_01", "--port=%d" % OF_PORT,
               "adaptive_lb", "--mode=%s" % self.mode,
               "--vip=%s" % VIP, "--servers=%s" % servers,
               "--service_port=%d" % SERVICE_PORT,
               "--report_port=%d" % REPORT_PORT,
               "--overload_ms=%s" % self.overload_ms,
               "--link_mbps=10", "--events=%s" % self.events_path]

        log = open(os.path.join(self.out_dir, "pox.log"), "w")
        self.pox = subprocess.Popen(cmd, cwd=self.pox_dir, env=env,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    preexec_fn=os.setsid)

        if not self.wait_for(lambda: ":%d " % OF_PORT in sh("ss -ltn"), 15):
            raise RuntimeError("POX did not start; see pox.log")

    def start_network(self):

        topo = AdaptiveLoadBalancerTopo(use_bw=self.use_bw)
        self.net = Mininet(topo=topo, switch=OVSSwitch,
                           link=TCLink if self.use_bw else Link,
                           controller=None, autoSetMacs=True, build=False)
        self.net.addController("c0", controller=RemoteController,
                               ip="127.0.0.1", port=OF_PORT)
        self.net.build()
        self.net.start()

    def start_server(self, name, delay, extra_args=()):

        host = self.net.get(name)
        log = open(os.path.join(self.out_dir, "server_%s.log" % name), "a")
        cmd = ["python3", "-u", os.path.join(APP, "backend_server.py"),
               "--name", name, "--port", str(SERVICE_PORT),
               "--delay", str(delay), "--workers", str(self.workers),
               "--report-to", "%s:%d" % (CONTROLLER_HOST[1], REPORT_PORT)]
        cmd += list(extra_args)
        self.servers[name] = host.popen(cmd, stdout=log,
                                        stderr=subprocess.STDOUT)
        return self.servers[name]

    def kill_server(self, name):

        proc = self.servers.pop(name, None)

        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()

    def popen(self, host_name, cmd, log_name):

        host = self.net.get(host_name)
        log = open(os.path.join(self.out_dir, log_name), "a")
        proc = host.popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        self.extra.append(proc)
        return proc

    def stop(self):

        for proc in list(self.servers.values()) + self.extra:
            if proc.poll() is None:
                proc.kill()

        if self.net is not None:
            try:
                self.net.stop()
            except Exception as error:   # keep going, cleanup follows
                print("net.stop failed:", error)

        if self.pox is not None and self.pox.poll() is None:
            os.killpg(os.getpgid(self.pox.pid), signal.SIGTERM)
            try:
                self.pox.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.pox.pid), signal.SIGKILL)

        full_cleanup()

    # ------------------------------------------------------------ helpers

    def client_cmd(self, out_csv, threads, duration=0, requests=0,
                   vip=VIP, size=2000, work_ms=0, start_at=0, name=None,
                   timeout=2.0, retries=1):

        cmd = ["python3", "-u", os.path.join(APP, "client.py"),
               "--vip", vip, "--port", str(SERVICE_PORT),
               "--threads", str(threads), "--size", str(size),
               "--work-ms", str(work_ms), "--timeout", str(timeout),
               "--retries", str(retries), "--out", out_csv,
               "--summary", out_csv.replace(".csv", "_summary.json")]

        if duration:
            cmd += ["--duration", str(duration)]
        else:
            cmd += ["--requests", str(requests)]

        if start_at:
            cmd += ["--start-at", "%.3f" % start_at]

        if name:
            cmd += ["--name", name]

        return cmd

    def dump_flows(self, path):

        text = sh("ovs-ofctl dump-flows s1")

        with open(path, "w") as f:
            f.write(text)

        return text

    def events(self):
        return read_events(self.events_path)

    def wait_event(self, pred, timeout):
        return self.wait_for(lambda: any(pred(e) for e in self.events()),
                             timeout)

    @staticmethod
    def wait_for(cond, timeout, step=0.2):

        end = time.time() + timeout

        while True:
            if cond():
                return True
            if time.time() >= end:
                return False
            time.sleep(step)


def read_events(path):

    events = []

    if not os.path.exists(path):
        return events

    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass

    return events


def client_hosts():
    return [name for name, _ in CLIENTS]
