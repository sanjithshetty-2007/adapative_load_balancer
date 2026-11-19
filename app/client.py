#!/usr/bin/env python3
"""
Concurrent TCP load generator.

Starts --threads worker threads. Each thread repeatedly opens a NEW TCP
connection to the virtual IP, sends one request, reads the whole reply and
records how long it took. Every connection is a new flow, so every request
is placed by the SDN controller.

Each request is written to a CSV file:
  t_start, t_end, client, thread, req_id, server, latency_ms, ok,
  attempts, error, peer

A failed attempt (refused, reset, timeout, short reply) is retried up to
--retries times. latency_ms covers all attempts, as the user would feel it.

Examples:
  python3 client.py --vip 10.0.0.100 --threads 8 --duration 20 --out a.csv
  python3 client.py --vip 10.0.0.100 --threads 4 --requests 25 --out b.csv
"""

import argparse
import csv
import json
import socket
import statistics
import threading
import time


def one_attempt(host, port, req_id, size, work_ms, timeout):
    """Returns (server name, peer ip). Raises OSError/ValueError on failure."""

    with socket.create_connection((host, port), timeout=timeout) as sock:

        sock.settimeout(timeout)
        peer = sock.getpeername()[0]
        sock.sendall(f"REQ {req_id} {size} {work_ms}\n".encode("ascii"))

        reader = sock.makefile("rb")
        header = reader.readline(4096).decode("ascii", "replace").split()

        if len(header) != 4 or header[0] != "RESP" or header[1] != str(req_id):
            raise ValueError("bad reply header: %r" % header)

        expected = int(header[3])
        received = 0

        while received < expected:
            chunk = reader.read(min(65536, expected - received))
            if not chunk:
                raise ValueError("short reply %d/%d" % (received, expected))
            received += len(chunk)

        return header[2], peer


def worker(args, thread_no, deadline, results, lock, counter):

    while True:

        with lock:
            if args.requests and counter[0] >= args.requests * args.threads:
                return
            counter[0] += 1
            req_id = counter[0]

        if deadline and time.time() >= deadline:
            return

        t_start = time.time()
        attempts = 0
        server = peer = ""
        error = ""
        ok = 0

        while attempts <= args.retries:

            attempts += 1

            try:
                server, peer = one_attempt(args.vip, args.port, req_id,
                                           args.size, args.work_ms,
                                           args.timeout)
                ok = 1
                error = ""
                break

            except (OSError, ValueError) as exc:
                error = type(exc).__name__ + ": " + str(exc)[:80]
                time.sleep(args.retry_wait)

        t_end = time.time()

        with lock:
            results.append([round(t_start, 4), round(t_end, 4), args.name,
                            thread_no, req_id, server,
                            round(1000 * (t_end - t_start), 2), ok,
                            attempts, error, peer])

        if args.think > 0:
            time.sleep(args.think)


def progress(results, lock, start, stop, every=2.0):
    """Print one line every `every` seconds: what finished in that interval."""

    seen = 0

    while not stop.wait(every):

        with lock:
            new = results[seen:]
            seen = len(results)

        ok = [r for r in new if r[7] == 1]
        per = {}
        for r in ok:
            per[r[5]] = per.get(r[5], 0) + 1
        avg = statistics.mean(r[6] for r in ok) if ok else 0.0
        servers = "  ".join("%s %3d" % (s, per.get(s, 0))
                            for s in sorted(set(per) | {"h1", "h2", "h3"}))

        print("%4.0fs | last %.0fs: %3d ok %3d failed | %s | avg %4.0f ms"
              % (time.time() - start, every, len(ok), len(new) - len(ok),
                 servers, avg), flush=True)


def summarize(rows, wall_s):

    ok_rows = [r for r in rows if r[7] == 1]
    lat = sorted(r[6] for r in ok_rows)
    per_server = {}

    for r in ok_rows:
        per_server[r[5]] = per_server.get(r[5], 0) + 1

    def pct(p):
        if not lat:
            return 0.0
        return lat[min(len(lat) - 1, int(round(p / 100.0 * (len(lat) - 1))))]

    return {
        "requests": len(rows),
        "ok": len(ok_rows),
        "failed": len(rows) - len(ok_rows),
        "retried": sum(1 for r in rows if r[8] > 1),
        "avg_ms": round(statistics.mean(lat), 1) if lat else 0.0,
        "p50_ms": round(pct(50), 1),
        "p95_ms": round(pct(95), 1),
        "max_ms": round(lat[-1], 1) if lat else 0.0,
        "throughput_rps": round(len(ok_rows) / wall_s, 1) if wall_s else 0.0,
        "per_server": dict(sorted(per_server.items())),
    }


def main():

    parser = argparse.ArgumentParser(description="TCP load generator")
    parser.add_argument("--vip", default="10.0.0.100")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--duration", type=float, default=0,
                        help="Run for this many seconds (0 = use --requests)")
    parser.add_argument("--requests", type=int, default=0,
                        help="Requests per thread when --duration is 0")
    parser.add_argument("--size", type=int, default=2000,
                        help="Reply size in bytes")
    parser.add_argument("--work-ms", type=float, default=0,
                        help="Extra work the server should do per request")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-wait", type=float, default=0.2)
    parser.add_argument("--think", type=float, default=0.0,
                        help="Pause between requests of one thread (s)")
    parser.add_argument("--start-at", type=float, default=0,
                        help="Wait until this Unix time before starting")
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--out", default="")
    parser.add_argument("--summary", default="",
                        help="Write the summary as JSON to this file")
    parser.add_argument("--progress", action="store_true",
                        help="Print a line every 2 s showing which servers "
                             "answered, and a readable summary at the end")
    args = parser.parse_args()

    if not args.duration and not args.requests:
        args.requests = 10

    if args.start_at:
        time.sleep(max(0.0, args.start_at - time.time()))

    results, lock, counter = [], threading.Lock(), [0]
    start = time.time()
    deadline = start + args.duration if args.duration else 0

    threads = [threading.Thread(target=worker,
                                args=(args, i, deadline, results, lock,
                                      counter))
               for i in range(args.threads)]

    stop = threading.Event()

    if args.progress:
        print("Client %s: %d threads -> %s:%d, one new TCP connection per "
              "request" % (args.name, args.threads, args.vip, args.port),
              flush=True)
        threading.Thread(target=progress, args=(results, lock, start, stop),
                         daemon=True).start()

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    stop.set()
    wall = time.time() - start
    results.sort(key=lambda r: r[0])

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_start", "t_end", "client", "thread", "req_id",
                        "server", "latency_ms", "ok", "attempts", "error",
                        "peer"])
            w.writerows(results)

    summary = summarize(results, wall)
    summary["client"] = args.name

    if args.summary:
        with open(args.summary, "w") as f:
            json.dump(summary, f, indent=2)

    if args.progress:
        print("\nDone in %.0f s: %d requests, %d ok, %d failed, %d needed a "
              "retry" % (wall, summary["requests"], summary["ok"],
                         summary["failed"], summary["retried"]))
        print("Latency: average %.0f ms, p95 %.0f ms. Throughput %.0f req/s"
              % (summary["avg_ms"], summary["p95_ms"],
                 summary["throughput_rps"]))
        print("Served by: " + ", ".join(
            "%s %d" % kv for kv in summary["per_server"].items()), flush=True)
    else:
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
