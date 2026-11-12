#!/usr/bin/env python3
"""
Background traffic generator, used to create NETWORK load in experiments.

  sink:  python3 traffic_gen.py --sink --port 5001
  send:  python3 traffic_gen.py --send 10.0.0.7:5001 --seconds 15

The sender pushes data over TCP as fast as the link allows, which fills
the sender's link. The controller sees this in the switch port counters.
"""

import argparse
import socket
import threading
import time


def sink(port):

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)

    def drain(conn):
        with conn:
            while conn.recv(65536):
                pass

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=drain, args=(conn,), daemon=True).start()


def send(target, seconds, streams):

    host, port = target.rsplit(":", 1)
    payload = b"z" * 65536
    end = time.time() + seconds
    sent = [0]
    lock = threading.Lock()

    def one():
        try:
            with socket.create_connection((host, int(port)), timeout=5) as s:
                while time.time() < end:
                    s.sendall(payload)
                    with lock:
                        sent[0] += len(payload)
        except OSError as error:
            print("traffic_gen send error:", error, flush=True)

    threads = [threading.Thread(target=one) for _ in range(streams)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("traffic_gen sent %.1f MB in %.0f s" % (sent[0] / 1e6, seconds),
          flush=True)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--sink", action="store_true")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--send", default="")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--streams", type=int, default=2)
    args = parser.parse_args()

    if args.sink:
        sink(args.port)
    elif args.send:
        send(args.send, args.seconds, args.streams)
    else:
        parser.error("use --sink or --send host:port")


if __name__ == "__main__":
    main()
