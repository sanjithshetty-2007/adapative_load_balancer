#!/usr/bin/env python3
"""
Socket tests on localhost (no Mininet needed).

Starts real backend_server.py processes and a fake controller that
collects load reports, then runs client.py against them.
"""

import csv
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, os.path.join(ROOT, "controller"))

from lb_core import parse_load_line  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


class FakeController:
    """Accepts load-report connections and keeps the last report per server."""

    def __init__(self):
        self.port = free_port()
        self.reports = {}
        self.lines = 0
        self.closed = set()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(conn,),
                             daemon=True).start()

    def _read(self, conn):
        # Same pattern as the real controller: a reset counts as closed.
        name = None
        try:
            for line in conn.makefile("r"):
                parsed = parse_load_line(line)
                if parsed:
                    name, fields = parsed
                    self.reports[name] = fields
                    self.lines += 1
        except OSError:
            pass
        if name:
            self.closed.add(name)


def start_server(name, port, delay, workers, report_port=None):
    cmd = [sys.executable, os.path.join(APP, "backend_server.py"),
           "--name", name, "--port", str(port), "--delay", str(delay),
           "--workers", str(workers), "--report-interval", "0.2"]
    if report_port:
        cmd += ["--report-to", "127.0.0.1:%d" % report_port]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if not wait_port(port):
        proc.kill()
        raise RuntimeError("server did not start")
    return proc


def run_client(port, out, **kw):
    cmd = [sys.executable, os.path.join(APP, "client.py"),
           "--vip", "127.0.0.1", "--port", str(port), "--out", out]
    for k, v in kw.items():
        cmd += ["--" + k.replace("_", "-"), str(v)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return json.loads(res.stdout.strip().splitlines()[-1])


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class SocketTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.ctl = FakeController()
        cls.port = free_port()
        cls.proc = start_server("s1", cls.port, 0.05, 4, cls.ctl.port)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()

    def test_1_single_request(self):
        out = os.path.join(self.tmp, "single.csv")
        summary = run_client(self.port, out, threads=1, requests=1)
        self.assertEqual(summary["ok"], 1)
        row = read_csv(out)[0]
        self.assertEqual(row["server"], "s1")
        self.assertGreaterEqual(float(row["latency_ms"]), 50)

    def test_2_concurrent_requests(self):
        out = os.path.join(self.tmp, "conc.csv")
        summary = run_client(self.port, out, threads=16, requests=10,
                             size=20000)
        self.assertEqual(summary["requests"], 160)
        self.assertEqual(summary["failed"], 0)

    def test_3_worker_limit_causes_queueing(self):
        # 4 workers, 50 ms each: 4 threads see ~50 ms, 16 threads must queue.
        light = run_client(self.port, os.path.join(self.tmp, "l.csv"),
                           threads=4, requests=10)
        heavy = run_client(self.port, os.path.join(self.tmp, "h.csv"),
                           threads=16, requests=10)
        self.assertLess(light["avg_ms"], 120)
        self.assertGreater(heavy["avg_ms"], 1.8 * light["avg_ms"])

    def test_4_load_reports_reach_controller(self):
        time.sleep(0.5)
        self.assertIn("s1", self.ctl.reports)
        r = self.ctl.reports["s1"]
        for key in ("workers", "active", "queued", "svc_ms", "avg_ms", "rps"):
            self.assertIn(key, r)
        self.assertEqual(r["workers"], "4")
        self.assertGreater(self.ctl.lines, 2)

    def test_5_reports_show_load_while_busy(self):
        seen = []

        def watch():
            end = time.time() + 2
            while time.time() < end:
                seen.append(int(self.ctl.reports["s1"]["queued"]))
                time.sleep(0.05)

        w = threading.Thread(target=watch)
        w.start()
        run_client(self.port, os.path.join(self.tmp, "busy.csv"),
                   threads=20, duration=2)
        w.join()
        self.assertGreater(max(seen), 4)

    def test_6_http_get_for_curl(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port,
                                    timeout=5) as resp:
            body = resp.read().decode()
        self.assertIn("Served by: s1", body)

    def test_7_bad_request_does_not_crash(self):
        with socket.create_connection(("127.0.0.1", self.port)) as s:
            s.sendall(b"HELLO\n")
            self.assertTrue(s.recv(100).startswith(b"ERR"))
        out = os.path.join(self.tmp, "after.csv")
        self.assertEqual(run_client(self.port, out, threads=1,
                                    requests=1)["ok"], 1)


class FailureTests(unittest.TestCase):

    def test_dead_server_counted_as_failure(self):
        tmp = tempfile.mkdtemp()
        port = free_port()          # nothing listens here
        summary = run_client(port, os.path.join(tmp, "dead.csv"), threads=2,
                             requests=3, timeout=0.5, retries=1)
        self.assertEqual(summary["failed"], 6)
        rows = read_csv(os.path.join(tmp, "dead.csv"))
        self.assertTrue(all(r["attempts"] == "2" for r in rows))

    def test_report_link_closes_when_server_dies(self):
        ctl = FakeController()
        port = free_port()
        proc = start_server("s9", port, 0.01, 2, ctl.port)
        time.sleep(0.6)
        self.assertIn("s9", ctl.reports)
        proc.kill()
        proc.wait()
        time.sleep(0.5)
        self.assertIn("s9", ctl.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
