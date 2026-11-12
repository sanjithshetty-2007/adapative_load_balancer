#!/usr/bin/env python3
"""
Decision logic of the SDN load balancer.

This module has no POX imports, so it can be unit tested on any machine.
The POX module (adaptive_lb.py) feeds it events (load reports, port
statistics, port up/down) and asks it which server should get each new
TCP connection.

Two modes:
  rr        Static round robin. Ignores load and health completely.
            This is the baseline used for the comparison.
  adaptive  Picks the healthy server with the lowest expected completion
            time, using the servers' own load reports and the network load
            measured from switch port counters. Failed servers are never
            picked; overloaded servers are heavily penalised.
"""

import threading
import time


class ServerState:

    def __init__(self, name, ip):

        self.name = name
        self.ip = ip

        # Values reported by the server itself (LOAD messages).
        self.workers = 1
        self.active = 0          # connections in progress (queued + working)
        self.queued = 0          # connections waiting for a free worker
        self.svc_ms = 0.0        # average pure service time (no queueing)
        self.avg_ms = 0.0        # average response time (with queueing)
        self.rps = 0.0
        self.last_report = None

        # Values measured by the controller.
        self.util = 0.0          # link utilisation of the server's port, 0..1
        self.assigned_since_report = 0
        self.assigned_total = 0
        self.est = 0.0           # estimated connections in progress now
        self.est_time = None     # when est was last brought up to date

        # Health.
        self.reporting = False   # receiving load reports
        self.port_up = True      # switch port is up
        self.overloaded = False
        self.overload_reason = ""

    @property
    def up(self):
        return self.reporting and self.port_up

    def snapshot(self):

        return {
            "name": self.name,
            "up": self.up,
            "overloaded": self.overloaded,
            "active": self.active,
            "queued": self.queued,
            "svc_ms": round(self.svc_ms, 1),
            "avg_ms": round(self.avg_ms, 1),
            "util": round(self.util, 3),
            "est": round(self.est, 1),
            "assigned_total": self.assigned_total,
        }


class Pool:

    def __init__(self, servers, mode="adaptive",
                 overload_ms=250.0, overload_queue=2.0, overload_util=0.8,
                 recover_factor=0.7, heartbeat_timeout=2.0,
                 overload_penalty=3.0):
        """
        servers: list of (name, ip) tuples.
        overload_ms: a server whose average response time is above this
            is overloaded.
        overload_queue: a server whose queue is longer than this many times
            its worker count is overloaded.
        overload_util: a server whose link is busier than this fraction of
            its capacity is overloaded.
        recover_factor: an overloaded server returns to normal only when all
            its values are below recover_factor * threshold (hysteresis, so
            it does not flip back and forth).
        heartbeat_timeout: seconds without a load report before a server is
            considered failed.
        overload_penalty: an overloaded server's expected time is multiplied
            by this. It is not cut off completely: a server that gets no
            traffic at all cannot show that it has recovered (its average
            response time would never change).
        """

        if mode not in ("rr", "adaptive"):
            raise ValueError("mode must be 'rr' or 'adaptive'")

        self.mode = mode
        self.servers = [ServerState(n, ip) for n, ip in servers]
        self.by_name = {s.name: s for s in self.servers}
        self.by_ip = {s.ip: s for s in self.servers}

        self.overload_ms = overload_ms
        self.overload_queue = overload_queue
        self.overload_util = overload_util
        self.recover_factor = recover_factor
        self.heartbeat_timeout = heartbeat_timeout
        self.overload_penalty = overload_penalty

        self.rr_index = 0
        self.lock = threading.RLock()

    # ------------------------------------------------------------ events

    def on_report(self, name, fields, now=None):
        """A LOAD message arrived. Returns a list of event dicts."""

        now = time.time() if now is None else now
        events = []

        with self.lock:

            s = self.by_name.get(name)

            if s is None:
                return events

            s.workers = max(1, int(fields.get("workers", s.workers)))
            s.active = int(fields.get("active", 0))
            s.queued = int(fields.get("queued", 0))
            s.svc_ms = float(fields.get("svc_ms", 0.0))
            s.avg_ms = float(fields.get("avg_ms", 0.0))
            s.rps = float(fields.get("rps", 0.0))
            s.last_report = now
            s.assigned_since_report = 0

            # The report is the truth: reset the estimate to it. It counts
            # every connection on the server, including traffic that did
            # not come through the VIP.
            s.est = float(s.active)
            s.est_time = now

            if not s.reporting:
                s.reporting = True
                if s.up:
                    events.append({"ev": "server_up", "server": name,
                                   "reason": "load reports received"})

            events += self._update_overload(s)

        return events

    def on_report_closed(self, name, now=None):
        """The server's report connection closed (process died)."""

        with self.lock:

            s = self.by_name.get(name)

            if s is None or not s.reporting:
                return []

            was_up = s.up
            s.reporting = False

            if was_up:
                return [{"ev": "server_down", "server": name,
                         "reason": "report connection closed"}]

        return []

    def on_port_util(self, name, util, now=None):
        """New link utilisation (0..1) measured from port statistics."""

        with self.lock:

            s = self.by_name.get(name)

            if s is None:
                return []

            s.util = max(0.0, float(util))

            return self._update_overload(s)

    def on_port_status(self, name, is_up, now=None):
        """The switch port of a server went down or came back up."""

        with self.lock:

            s = self.by_name.get(name)

            if s is None or s.port_up == is_up:
                return []

            was_up = s.up
            s.port_up = is_up

            if was_up and not s.up:
                return [{"ev": "server_down", "server": name,
                         "reason": "switch port down"}]

            if not was_up and s.up:
                return [{"ev": "server_up", "server": name,
                         "reason": "switch port up"}]

        return []

    def check_timeouts(self, now=None):
        """Call about once a second. Detects silent (hung/unreachable) servers."""

        now = time.time() if now is None else now
        events = []

        with self.lock:

            for s in self.servers:

                if (s.reporting and s.last_report is not None
                        and now - s.last_report > self.heartbeat_timeout):

                    was_up = s.up
                    s.reporting = False

                    if was_up:
                        events.append({
                            "ev": "server_down", "server": s.name,
                            "reason": "no load report for %.1f s"
                                      % (now - s.last_report)})

        return events

    # ------------------------------------------------------------ overload

    def _peers(self, s):
        """Average avg_ms and queue of the other healthy servers."""

        others = [o for o in self.servers if o is not s and o.up]

        if not others:
            return None

        return (sum(o.avg_ms for o in others) / len(others),
                sum(o.queued for o in others) / float(len(others)))

    def _worse_than_peers(self, s, slow, long_queue):
        """
        A busy server only counts as overloaded when it is clearly worse
        than the rest of the pool. When every server is equally busy,
        moving traffic around cannot help, and flagging them one after
        another would just push the load back and forth.
        """

        peers = self._peers(s)

        if peers is None:
            return slow, long_queue

        peer_ms, peer_queue = peers

        return (slow and s.avg_ms > 1.5 * peer_ms,
                long_queue and s.queued > peer_queue + s.workers)

    def _update_overload(self, s):

        if s.workers <= 0:
            return []

        queue_ratio = s.queued / float(s.workers)

        if not s.overloaded:

            reasons = []
            slow, long_queue = self._worse_than_peers(
                s, s.avg_ms > self.overload_ms,
                queue_ratio > self.overload_queue)

            if slow:
                reasons.append("avg response %.0f ms > %.0f ms"
                               % (s.avg_ms, self.overload_ms))

            if long_queue:
                reasons.append("queue %d > %.1f x %d workers"
                               % (s.queued, self.overload_queue, s.workers))

            if s.util > self.overload_util:
                reasons.append("link %.0f%% > %.0f%%"
                               % (100 * s.util, 100 * self.overload_util))

            if reasons:
                s.overloaded = True
                s.overload_reason = "; ".join(reasons)
                return [{"ev": "overloaded", "server": s.name,
                         "reason": s.overload_reason}]

        else:

            f = self.recover_factor
            peers = self._peers(s)
            # Also normal again once it is no worse than the others.
            like_peers = (peers is not None
                          and s.avg_ms < 1.2 * peers[0]
                          and s.queued <= peers[1] + 1)

            if (s.util < f * self.overload_util
                    and (like_peers
                         or (s.avg_ms < f * self.overload_ms
                             and queue_ratio < f * self.overload_queue))):

                s.overloaded = False
                s.overload_reason = ""
                return [{"ev": "normal", "server": s.name,
                         "reason": "load back below thresholds"}]

        return []

    # ------------------------------------------------------------ choice

    def estimate(self, s, now):
        """
        Connections in progress on s right now (estimated).

        Reports arrive only every 0.5 s, and in that time a server
        finishes many connections. So between reports the controller
        keeps its own running estimate: it starts from the reported count,
        adds every connection it sends to s, and subtracts the ones s
        should have finished meanwhile (each busy worker finishes one
        connection per svc_ms). The next report corrects any drift.

        (A first version only added new connections and never subtracted
        finished ones. The server that had just reported then looked
        almost idle next to the others and received a burst.)
        """

        if s.est_time is None:
            s.est_time = now

        dt = now - s.est_time

        if dt > 0 and s.est > 0:
            busy = min(s.est, float(s.workers))
            done = dt * busy / (max(s.svc_ms, 1.0) / 1000.0)
            s.est = max(0.0, s.est - done)

        s.est_time = max(s.est_time, now)

        return s.est

    def score(self, s, now=None):
        """
        Expected time (ms) a new connection would take on server s.

        outstanding work / workers * service time, inflated when the
        server's link is busy (a busy link delays every response).
        """

        now = time.time() if now is None else now
        outstanding = self.estimate(s, now) + 1
        svc = max(s.svc_ms, 1.0)
        base = outstanding / float(s.workers) * svc
        cost = base * (1.0 + 4.0 * s.util * s.util)

        if s.overloaded:
            cost *= self.overload_penalty

        return cost

    def choose(self, now=None):
        """
        Pick a server for a new connection.
        Returns (ServerState or None, info dict).
        """

        now = time.time() if now is None else now

        with self.lock:

            if self.mode == "rr":

                s = self.servers[self.rr_index % len(self.servers)]
                self.rr_index += 1
                s.assigned_total += 1

                return s, {"mode": "rr"}

            healthy = [s for s in self.servers if s.up]

            if not healthy:
                return None, {"mode": "adaptive", "reason": "no healthy server"}

            candidates = healthy
            fallback = all(s.overloaded for s in healthy)

            scores = {s.name: self.score(s, now) for s in candidates}
            best = min(scores.values())

            # Break ties in turn, so equal servers share the work evenly.
            tied = [s for s in candidates if scores[s.name] <= best * 1.0001]
            s = tied[self.rr_index % len(tied)]
            self.rr_index += 1

            s.assigned_since_report += 1
            s.assigned_total += 1
            s.est += 1.0

            info = {"mode": "adaptive",
                    "score": round(scores[s.name], 1),
                    "scores": {k: round(v, 1) for k, v in scores.items()}}

            if fallback:
                info["note"] = "all servers overloaded"

            return s, info

    def snapshot(self):

        with self.lock:
            return [s.snapshot() for s in self.servers]


def parse_load_line(line):
    """
    'LOAD h1 workers=4 active=3 queued=0 svc_ms=50.2 avg_ms=51.0 rps=12.5'
    -> ('h1', {'workers': '4', ...}) or None if the line is not valid.
    """

    parts = line.strip().split()

    if len(parts) < 2 or parts[0] != "LOAD":
        return None

    fields = {}

    for item in parts[2:]:

        if "=" in item:
            key, value = item.split("=", 1)
            fields[key] = value

    return parts[1], fields
