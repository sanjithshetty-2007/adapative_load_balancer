#!/usr/bin/env python3
"""Unit tests for the controller's decision logic (no Mininet needed)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "controller"))

from lb_core import Pool, parse_load_line  # noqa: E402


SERVERS = [("h1", "10.0.0.1"), ("h2", "10.0.0.2"), ("h3", "10.0.0.3")]


def report(pool, name, now=100.0, **kw):
    fields = {"workers": 4, "active": 0, "queued": 0,
              "svc_ms": 50, "avg_ms": 50, "rps": 0}
    fields.update(kw)
    return pool.on_report(name, fields, now=now)


def all_up(pool, now=100.0):
    for name, _ in SERVERS:
        report(pool, name, now=now)


class RoundRobinTests(unittest.TestCase):

    def test_cycles_in_order(self):
        pool = Pool(SERVERS, mode="rr")
        picks = [pool.choose()[0].name for _ in range(6)]
        self.assertEqual(picks, ["h1", "h2", "h3", "h1", "h2", "h3"])

    def test_ignores_load_and_health(self):
        pool = Pool(SERVERS, mode="rr")
        all_up(pool)
        report(pool, "h1", active=50, queued=40, avg_ms=900)
        pool.on_report_closed("h2")
        picks = [pool.choose()[0].name for _ in range(3)]
        self.assertEqual(picks, ["h1", "h2", "h3"])


class AdaptiveTests(unittest.TestCase):

    def test_no_server_until_reports(self):
        pool = Pool(SERVERS, mode="adaptive")
        server, info = pool.choose()
        self.assertIsNone(server)

    def test_equal_servers_share_evenly(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        picks = [pool.choose()[0].name for _ in range(30)]
        for name, _ in SERVERS:
            self.assertEqual(picks.count(name), 10)

    def test_prefers_least_loaded(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        report(pool, "h1", active=4)
        report(pool, "h2", active=1)
        report(pool, "h3", active=3)
        self.assertEqual(pool.choose(now=100.0)[0].name, "h2")

    def test_estimate_drains_between_reports(self):
        # 4 workers, 50 ms each: 8 connections take about 100 ms.
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        report(pool, "h1", now=100.0, active=8)
        h1 = pool.by_name["h1"]
        self.assertAlmostEqual(pool.estimate(h1, 100.05), 4.0, places=1)
        self.assertEqual(pool.estimate(h1, 100.5), 0.0)

    def test_prefers_faster_server(self):
        pool = Pool(SERVERS, mode="adaptive")
        report(pool, "h1", svc_ms=20)
        report(pool, "h2", svc_ms=50)
        report(pool, "h3", svc_ms=150)
        picks = [pool.choose()[0].name for _ in range(30)]
        self.assertGreater(picks.count("h1"), picks.count("h2"))
        self.assertGreater(picks.count("h2"), picks.count("h3"))

    def test_counts_assignments_between_reports(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        # Without counting new assignments a burst would all go to one server.
        picks = {pool.choose()[0].name for _ in range(3)}
        self.assertEqual(picks, {"h1", "h2", "h3"})

    def test_fresh_report_does_not_attract_a_burst(self):
        # Regression: the server that reported last used to look idle
        # (its count was reset) and took every new connection.
        pool = Pool(SERVERS, mode="adaptive")
        now = 200.0
        for name, _ in SERVERS:
            report(pool, name, now=now, svc_ms=50, avg_ms=80)
        for i in range(60):                   # 60 picks over 0.6 s
            pool.choose(now=now + i * 0.01)
        report(pool, "h3", now=now + 0.6, active=4, svc_ms=50, avg_ms=80)
        picks = [pool.choose(now=now + 0.6 + i * 0.01)[0].name
                 for i in range(12)]
        for name, _ in SERVERS:
            self.assertLessEqual(picks.count(name), 6, picks)

    def test_counts_load_not_sent_by_controller(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        report(pool, "h1", active=10)       # e.g. clients using h1's own IP
        picks = [pool.choose(now=100.0)[0].name for _ in range(12)]
        self.assertLess(picks.count("h1"), 2)

    def test_busy_link_is_avoided(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        pool.on_port_util("h1", 0.6)
        picks = [pool.choose()[0].name for _ in range(12)]
        self.assertLess(picks.count("h1"), 4)


class OverloadTests(unittest.TestCase):

    def test_slow_server_marked_overloaded_and_skipped(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        events = report(pool, "h1", avg_ms=600)
        self.assertEqual(events[0]["ev"], "overloaded")
        picks = [pool.choose()[0].name for _ in range(30)]
        self.assertLessEqual(picks.count("h1"), 4)

    def test_overloaded_server_still_gets_a_trickle(self):
        # Fully cutting it off would freeze its stats: it could never
        # show that it is fine again.
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        report(pool, "h1", avg_ms=600)
        picks = [pool.choose()[0].name for _ in range(60)]
        self.assertGreater(picks.count("h1"), 0)

    def test_long_queue_is_overload(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        events = report(pool, "h2", queued=12)
        self.assertEqual(events[0]["ev"], "overloaded")

    def test_busy_link_is_overload(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        events = pool.on_port_util("h3", 0.95)
        self.assertEqual(events[0]["ev"], "overloaded")

    def test_hysteresis_before_normal(self):
        pool = Pool(SERVERS, mode="adaptive", overload_ms=250)
        all_up(pool)
        report(pool, "h1", avg_ms=400)
        # 200 ms is under the threshold but above 0.7 * 250: still overloaded.
        self.assertEqual(report(pool, "h1", avg_ms=200), [])
        self.assertTrue(pool.by_name["h1"].overloaded)
        events = report(pool, "h1", avg_ms=60)
        self.assertEqual(events[0]["ev"], "normal")

    def test_all_overloaded_still_serves(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        for name, _ in SERVERS:
            pool.on_port_util(name, 0.95)
        server, info = pool.choose()
        self.assertIsNotNone(server)
        self.assertIn("note", info)

    def test_equally_busy_pool_is_not_flagged(self):
        # Everyone slow and queueing: moving traffic cannot help, so no
        # server is singled out (this stopped load flapping between them).
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        events = []
        for avg_ms, queued in [(100, 3), (200, 6), (300, 9), (320, 10)]:
            for name, _ in SERVERS:
                events += report(pool, name, avg_ms=avg_ms, queued=queued)
        self.assertEqual(events, [])

    def test_back_to_normal_when_like_peers(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        report(pool, "h1", avg_ms=600, queued=12)
        report(pool, "h2", avg_ms=280, queued=9)
        report(pool, "h3", avg_ms=280, queued=9)
        events = report(pool, "h1", avg_ms=300, queued=9)
        self.assertEqual(events[0]["ev"], "normal")


class FailureTests(unittest.TestCase):

    def test_report_connection_closed(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        events = pool.on_report_closed("h2")
        self.assertEqual(events[0]["ev"], "server_down")
        picks = {pool.choose()[0].name for _ in range(20)}
        self.assertEqual(picks, {"h1", "h3"})

    def test_heartbeat_timeout(self):
        pool = Pool(SERVERS, mode="adaptive", heartbeat_timeout=2.0)
        all_up(pool, now=100.0)
        report(pool, "h1", now=103.0)
        report(pool, "h3", now=103.0)
        events = pool.check_timeouts(now=103.5)
        self.assertEqual([e["server"] for e in events], ["h2"])

    def test_port_down_and_up(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        down = pool.on_port_status("h3", False)
        self.assertEqual(down[0]["ev"], "server_down")
        self.assertNotIn("h3", {pool.choose()[0].name for _ in range(10)})
        up = pool.on_port_status("h3", True)
        self.assertEqual(up[0]["ev"], "server_up")

    def test_recovery_after_restart(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        pool.on_report_closed("h1")
        events = report(pool, "h1", now=110.0)
        self.assertEqual(events[0]["ev"], "server_up")

    def test_all_down(self):
        pool = Pool(SERVERS, mode="adaptive")
        all_up(pool)
        for name, _ in SERVERS:
            pool.on_report_closed(name)
        self.assertIsNone(pool.choose()[0])


class ParseTests(unittest.TestCase):

    def test_parse(self):
        name, fields = parse_load_line(
            "LOAD h1 workers=4 active=3 queued=0 svc_ms=50.2 avg_ms=51\n")
        self.assertEqual(name, "h1")
        self.assertEqual(fields["active"], "3")

    def test_reject(self):
        self.assertIsNone(parse_load_line("HELLO h1"))
        self.assertIsNone(parse_load_line(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
