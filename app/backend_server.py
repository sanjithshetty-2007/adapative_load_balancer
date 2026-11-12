#!/usr/bin/env python3
"""
Backend TCP application server.

Each connection carries one request. Two request formats are accepted:

  REQ <id> <size> [work_ms]\\n      (used by app/client.py)
      -> RESP <id> <server> <size>\\n followed by <size> bytes

  GET / HTTP/1.1 ...                (so you can test with curl)
      -> HTTP/1.1 200 OK with a short text body

The server has a fixed number of workers (--workers). A request first
waits for a free worker (queueing), then "works" for --delay seconds plus
the optional work_ms from the request. Response time therefore grows when
the server gets more requests than it can handle, like a real server.

Every --report-interval seconds the server sends a load report to the SDN
controller over a TCP socket:

  LOAD h1 workers=4 active=6 queued=2 svc_ms=50.3 avg_ms=81.2 rps=40.0
"""

import argparse
import collections
import socket
import socketserver
import threading
import time


class Stats:
    """Thread-safe counters used for the load reports."""

    def __init__(self, window=2.0):

        self.lock = threading.Lock()
        self.active = 0
        self.queued = 0
        self.served = 0
        self.window = window
        self.recent = collections.deque()   # (end time, response s, service s)

    def finished(self, response_s, service_s):

        now = time.time()

        with self.lock:
            self.served += 1
            self.recent.append((now, response_s, service_s))

    def report_fields(self, workers):

        now = time.time()

        with self.lock:

            while self.recent and now - self.recent[0][0] > self.window:
                self.recent.popleft()

            n = len(self.recent)

            if n:
                avg_ms = 1000 * sum(r[1] for r in self.recent) / n
                svc_ms = 1000 * sum(r[2] for r in self.recent) / n
            else:
                avg_ms = svc_ms = 0.0

            return {
                "workers": workers,
                "active": self.active,
                "queued": self.queued,
                "svc_ms": round(svc_ms, 1),
                "avg_ms": round(avg_ms, 1),
                "rps": round(n / self.window, 1),
                "served": self.served,
            }


class BackendHandler(socketserver.StreamRequestHandler):

    timeout = 10

    def handle(self):

        server = self.server
        stats = server.stats
        start = time.time()

        try:
            first = self.rfile.readline(4096)
        except OSError:
            return

        # Health checks may connect and close without sending data.
        if not first:
            return

        text = first.decode("utf-8", "replace").strip()

        if text.startswith("GET"):
            self._read_http_headers()
            kind, req_id, size, work_ms = "http", "-", 0, 0.0
        else:
            parts = text.split()
            if len(parts) < 3 or parts[0] != "REQ":
                self._safe_send(b"ERR bad request\n")
                return
            kind = "req"
            req_id = parts[1]
            try:
                size = max(0, min(int(parts[2]), 10_000_000))
                work_ms = float(parts[3]) if len(parts) > 3 else 0.0
            except ValueError:
                self._safe_send(b"ERR bad request\n")
                return

        with stats.lock:
            stats.active += 1
            stats.queued += 1

        acquired = False

        try:

            server.workers_sem.acquire()
            acquired = True

            with stats.lock:
                stats.queued -= 1

            work_start = time.time()
            time.sleep(server.delay_seconds + work_ms / 1000.0)

            if kind == "http":
                body = (
                    f"Served by: {server.backend_name}\n"
                    f"Request number: {stats.served + 1}\n"
                    f"Processing delay: {server.delay_seconds:.2f} seconds\n"
                ).encode("utf-8")
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n" + body
                )
            else:
                header = f"RESP {req_id} {server.backend_name} {size}\n"
                response = header.encode("ascii") + b"x" * size

            service_s = time.time() - work_start

        finally:

            if acquired:
                server.workers_sem.release()
            else:
                with stats.lock:
                    stats.queued -= 1

        sent = self._safe_send(response)

        with stats.lock:
            stats.active -= 1

        if sent:
            stats.finished(time.time() - start, service_s)

        if server.verbose:
            print(f"[{server.backend_name}] {kind} {req_id} done in "
                  f"{1000 * (time.time() - start):.0f} ms "
                  f"(active={stats.active})", flush=True)

    def _read_http_headers(self):

        for _ in range(100):
            line = self.rfile.readline(4096)
            if not line or line in (b"\r\n", b"\n"):
                break

    def _safe_send(self, data):

        try:
            self.request.sendall(data)
            return True
        except OSError as error:
            print(f"[{self.server.backend_name}] Error: {error}", flush=True)
            return False


class BackendServer(socketserver.ThreadingTCPServer):

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


def load_reporter(server, host, port, interval, stop):
    """Keep a TCP connection to the controller and send LOAD lines."""

    while not stop.is_set():

        try:

            with socket.create_connection((host, port), timeout=3) as sock:

                print(f"[{server.backend_name}] Reporting load to "
                      f"{host}:{port}", flush=True)

                while not stop.is_set():

                    fields = server.stats.report_fields(server.workers)
                    line = "LOAD " + server.backend_name + " " + " ".join(
                        f"{k}={v}" for k, v in fields.items()) + "\n"
                    sock.sendall(line.encode("ascii"))
                    stop.wait(interval)

        except OSError as error:

            print(f"[{server.backend_name}] Report link problem: {error}; "
                  f"retrying", flush=True)
            stop.wait(1.0)


def main():

    parser = argparse.ArgumentParser(
        description="Adaptive Load Balancer backend server")
    parser.add_argument("--name", required=True, help="Name of this backend")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port on which the server listens")
    parser.add_argument("--delay", type=float, default=0.05,
                        help="Simulated processing time per request (s)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Requests processed at the same time")
    parser.add_argument("--report-to", default="",
                        help="Controller address host:port for load reports "
                             "(empty = do not report)")
    parser.add_argument("--report-interval", type=float, default=0.5)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    server = BackendServer(("0.0.0.0", args.port), BackendHandler)
    server.backend_name = args.name
    server.delay_seconds = max(0.0, args.delay)
    server.workers = max(1, args.workers)
    server.workers_sem = threading.BoundedSemaphore(server.workers)
    server.stats = Stats()
    server.verbose = args.verbose

    stop = threading.Event()

    if args.report_to:
        host, port = args.report_to.rsplit(":", 1)
        threading.Thread(target=load_reporter,
                         args=(server, host, int(port),
                               args.report_interval, stop),
                         daemon=True).start()

    print(f"[{args.name}] Listening on port {args.port}; "
          f"delay={server.delay_seconds:.2f}s workers={server.workers}",
          flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{args.name}] Shutting down")
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
