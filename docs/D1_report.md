# D1 Report: Adaptive Load Balancer with SDN

CN mini project #20, Deliverable 1 (15 marks).
Architecture, TCP socket programs, Mininet + POX setup, socket–SDN
integration and the first end-to-end tests.

| Rubric item | Marks | Where |
|---|---|---|
| Problem understanding & architecture | 2 | [section 1](#1-problem-understanding-and-architecture) |
| TCP socket implementation | 4 | [section 2](#2-tcp-socket-implementation) |
| Mininet + SDN implementation | 4 | [section 3](#3-mininet--sdn-implementation) |
| Socket–SDN integration | 3 | [section 4](#4-socketsdn-integration) |
| Initial testing & demo | 2 | [section 5](#5-initial-testing-and-demo) |

---

## 1. Problem understanding and architecture

**Problem.** Several TCP application servers offer the same service. Client
traffic should be spread over them according to how loaded each server
and its network link is, not blindly. Overloaded or failed servers must be
noticed and avoided. A static policy (round robin) must be compared with
the adaptive one.

**Why SDN.** The switch does the forwarding at line rate. Only the first
packet of each connection goes to the controller, which has a global view
(every server's load, every port's traffic) and programs the switch with
flow rules. There is no proxy in the data path: clients talk to a
**virtual IP (VIP)**, and the switch rewrites addresses to the chosen
server.

**Required features and how they are met**

| Requirement | Implementation |
|---|---|
| Multiple TCP servers | `app/backend_server.py` on h1, h2, h3 (port 8080) |
| Concurrent client requests | `app/client.py`: N threads per host, on 4 client hosts at once |
| Monitor server load | servers send `LOAD` reports to the controller over TCP every 0.5 s |
| Monitor network load | controller polls OpenFlow port statistics every 1 s |
| Distribute flows using SDN | POX installs per-connection rewrite rules (VIP to server) |
| Detect overloaded servers | thresholds on response time, queue length, link use (D2) |
| Detect server failures | report socket closed / heartbeat timeout / port down (D2) |
| Compare static and adaptive | `--mode=rr` vs `--mode=adaptive`, same experiments (D2) |

**Architecture**

```
                         POX controller  (controller/adaptive_lb.py + lb_core.py)
                         +-------------------------------------------+
   LOAD reports -------> | report server :9000  -> server state      |
   (TCP, every 0.5 s)    | port stats every 1 s -> link utilisation  |
                         | PacketIn -> choose server -> 2 FlowMods   |
                         +---------------------+---------------------+
                                               | OpenFlow 1.0 (:6633)
   h4 10.0.0.4 ---\                     +------+------+       /--- h1 10.0.0.1 :8080
   h5 10.0.0.5 ----+---- 100 Mbit/s ----|     s1      |-------+--- h2 10.0.0.2 :8080
   h6 10.0.0.6 ----+                    |    (OVS)    | 10    \--- h3 10.0.0.3 :8080
   h7 10.0.0.7 ---/                     +------+------+ Mbit/s
     clients                                   |              TCP servers
                                  ctl 10.0.0.254 (root namespace)
           VIP 10.0.0.100 / 00:00:00:00:01:00 (answered by the controller)
```

Connection setup, step by step:

```
client h4            switch s1                 controller              server h2
   | ARP who-has VIP     |                          |                      |
   |-------------------->|---- PacketIn ----------->|                      |
   |<--------------------|<--- PacketOut (ARP reply: VIP MAC) ------------ |
   | TCP SYN to VIP:8080 |                          |                      |
   |-------------------->|---- PacketIn ----------->| choose h2            |
   |                     |<--- FlowMod reverse -----| (h2 -> h4: src=VIP)  |
   |                     |<--- FlowMod forward -----| (h4 -> VIP: dst=h2)  |
   |                     |<--- PacketOut (the SYN) -|                      |
   |                     |-------- SYN, dst rewritten to 10.0.0.2 -------->|
   |<-- SYN-ACK, src rewritten to VIP --------------------------------------|
   |  ... rest of the connection is switched in hardware, no controller ...|
```

## 2. TCP socket implementation

All socket programs use Python's `socket` / `socketserver` modules.

### Application server: `app/backend_server.py`

* `ThreadingTCPServer`: one thread per connection, so many clients are
  served at the same time.
* A `BoundedSemaphore(workers)` (default 4) limits how many requests are
  processed at once. Extra requests **wait in a queue**, as on a real
  server, so response time grows with load.
* Protocol (one request per connection):
  `REQ <id> <size> [work_ms]\n` gets the reply `RESP <id> <server> <size>\n`
  followed by `size` bytes. `work_ms` lets a test make a request heavier.
  HTTP `GET` is also answered, so `curl` works.
* Statistics over a 2 s window: active, queued, average service time,
  average response time, requests/s, total served.
* **Load reporter thread**: keeps one TCP connection to the controller
  and sends a `LOAD` line every 0.5 s. It reconnects if the link breaks.
* Robustness: bad requests get `ERR`, send errors are caught, and a
  client that closes early does not crash the server.

### Client / load generator: `app/client.py`

* N threads. Each opens a **new TCP connection per request** to the VIP,
  so every request is a new flow the controller must place.
* Timeout (2 s) and one retry. Every request is logged to CSV: start,
  end, server that answered, latency, ok, attempts, error, peer address.
  A JSON summary gives avg / p50 / p95 / max latency, throughput,
  requests per server and the failure and retry counts.
* `--duration` or `--requests`, `--work-ms`, `--start-at` (to start
  several hosts at the same moment).

### Network load generator: `app/traffic_gen.py`

Bulk TCP sender and sink (`--send ip:5001 --streams 4` / `--sink`), used to
saturate a server's 10 Mbit/s link.

### Socket tests (localhost, no Mininet): `tests/test_sockets.py`

Real TCP sockets on 127.0.0.1, with a stand-in controller that collects the load reports. **9 of 9 passed.**

| Group | Test | Result |
|---|---|---|
| Failure | `test_dead_server_counted_as_failure` | OK |
| Failure | `test_report_link_closes_when_server_dies` | OK |
| Socket | `test_1_single_request` | OK |
| Socket | `test_2_concurrent_requests` | OK |
| Socket | `test_3_worker_limit_causes_queueing` | OK |
| Socket | `test_4_load_reports_reach_controller` | OK |
| Socket | `test_5_reports_show_load_while_busy` | OK |
| Socket | `test_6_http_get_for_curl` | OK |
| Socket | `test_7_bad_request_does_not_crash` | OK |

## 3. Mininet + SDN implementation

### Topology: `topology/topology.py`

* 1 Open vSwitch `s1`, 3 servers (h1–h3), 4 clients (h4–h7).
* `TCLink`: server links are **10 Mbit/s** (queue 100 packets), client
  links 100 Mbit/s, so a server link can really become the bottleneck.
* Servers are attached first, so they sit on switch ports 1–3.
* `ctl` (10.0.0.254) is a host in the **root namespace**. The servers
  run in their own network namespaces and cannot reach 127.0.0.1 of the
  VM. Through `ctl` they reach the controller's report socket.
* The topology can be used with `sudo mn --custom topology/topology.py
  --topo adaptive ...`.

### Controller: `controller/adaptive_lb.py` (POX, OpenFlow 1.0)

| Function | OpenFlow messages |
|---|---|
| Answer ARP for the VIP | PacketIn (ARP request), then PacketOut (ARP reply with the VIP MAC) |
| Place a new TCP connection | PacketIn (SYN), then FlowMod reverse, FlowMod forward, PacketOut |
| L2 learning for all other traffic | PacketIn, then FlowMod `dl_dst -> port` (priority 10) |
| Monitor network load | `ofp_port_stats_request` every 1 s, then PortStatsReceived |
| Detect link failure | PortStatus (link down / port deleted) |
| Remove flows of a failed server | FlowMod `OFPFC_DELETE` |

Flow rules seen in the switch during the D1 test
(`results/d1/flows_during_load.txt`, shortened):

```
priority=1000,tcp,nw_src=10.0.0.4,nw_dst=10.0.0.100,tp_src=56990,tp_dst=8080
    actions=mod_dl_dst:00:00:00:00:00:02,mod_nw_dst:10.0.0.1,output:1     <- forward
priority=1000,tcp,nw_src=10.0.0.1,nw_dst=10.0.0.4,tp_src=8080,tp_dst=56990
    actions=mod_dl_src:00:00:00:00:01:00,mod_nw_src:10.0.0.100,output:4   <- reverse
priority=10,dl_dst=00:00:00:00:00:02 actions=output:1                     <- L2 learning
```

The reverse rule is installed **before** the forward rule, so the
server's SYN-ACK can never reach the switch before its rule exists.
Rules time out after 10 s idle, so finished connections clean
themselves up.

## 4. Socket–SDN integration

This is where the TCP programs and the SDN controller work together:

1. **Servers to controller (TCP socket).** Every server keeps a TCP
   connection to `10.0.0.254:9000`, the controller's report server (a
   thread inside the POX process), and sends
   `LOAD h1 workers=4 active=6 queued=2 svc_ms=50.3 avg_ms=81.2 rps=40.0 served=812`
   every 0.5 s. The controller parses it (`lb_core.parse_load_line`) and
   updates that server's state. The report thread hands events to POX's
   main loop with `core.callLater`, so flow changes always happen in the
   OpenFlow thread.
2. **Switch to controller (OpenFlow).** Port statistics give the bytes
   on each server port per second, which becomes **link utilisation**.
3. **Controller to switch (OpenFlow).** For every new client connection
   the controller combines (1) and (2) to pick a server and installs the
   rewrite rules. The client's socket only ever talks to the VIP.
4. **Client sockets to servers through SDN.** The client programs have no
   idea which server answers. The `peer` column in the client CSV is
   always 10.0.0.100, and the `server` column shows who actually served
   each request.

Every decision is written to a JSON-lines event log
(`controller_events.jsonl`): `load`, `port_stats`, `state`, `assign`,
`server_up/down`, `overloaded/normal`, `flows_deleted`. This makes the
integration visible and testable.

## 5. Initial testing and demo

### Unit tests of the decision logic: `tests/test_lb_core.py`

The controller's decisions are tested without Mininet: round robin, adaptive choice, overload, failure handling and message parsing. **26 of 26 passed** in the final run.

| Group | Test | Result |
|---|---|---|
| Adaptive | `test_busy_link_is_avoided` | OK |
| Adaptive | `test_counts_assignments_between_reports` | OK |
| Adaptive | `test_counts_load_not_sent_by_controller` | OK |
| Adaptive | `test_equal_servers_share_evenly` | OK |
| Adaptive | `test_estimate_drains_between_reports` | OK |
| Adaptive | `test_fresh_report_does_not_attract_a_burst` | OK |
| Adaptive | `test_no_server_until_reports` | OK |
| Adaptive | `test_prefers_faster_server` | OK |
| Adaptive | `test_prefers_least_loaded` | OK |
| Failure | `test_all_down` | OK |
| Failure | `test_heartbeat_timeout` | OK |
| Failure | `test_port_down_and_up` | OK |
| Failure | `test_recovery_after_restart` | OK |
| Failure | `test_report_connection_closed` | OK |
| Overload | `test_all_overloaded_still_serves` | OK |
| Overload | `test_back_to_normal_when_like_peers` | OK |
| Overload | `test_busy_link_is_overload` | OK |
| Overload | `test_equally_busy_pool_is_not_flagged` | OK |
| Overload | `test_hysteresis_before_normal` | OK |
| Overload | `test_long_queue_is_overload` | OK |
| Overload | `test_overloaded_server_still_gets_a_trickle` | OK |
| Overload | `test_slow_server_marked_overloaded_and_skipped` | OK |
| Parse | `test_parse` | OK |
| Parse | `test_reject` | OK |
| RoundRobin | `test_cycles_in_order` | OK |
| RoundRobin | `test_ignores_load_and_health` | OK |

### End-to-end tests in Mininet with POX: `tests/d1_mininet_tests.py`

These run in round robin mode (the simple, predictable policy) to show
that the whole SDN path works. Full output:
[results/d1/d1_test_report.md](../results/d1/d1_test_report.md).

**Result: 10 of 10 tests passed** (20 s).

| # | Test | Result | Evidence |
|---|---|---|---|
| T1 | All hosts reach each other through s1 (pingall) | PASS | packet loss 0.0% |
| T2 | curl to the virtual IP gets a server reply | PASS | Served by: h1 |
| T3 | Controller answers ARP for the VIP | PASS | 10.0.0.100 dev h4-eth0 lladdr 00:00:00:00:01:00 REACHABLE |
| T4 | Round robin spreads 30 requests over 3 servers | PASS | per server: {'h1': 10, 'h2': 10, 'h3': 10} |
| T5 | 4 client hosts x 8 threads x 10 requests at once | PASS | 320/320 succeeded, served by ['h1', 'h2', 'h3'] |
| T6 | Switch holds forward and reverse rewrite rules | PASS | 208 forward (VIP->server) and 209 reverse (server->VIP) rules seen |
| T7 | Clients only see the VIP, never a server IP | PASS | peer addresses seen: ['10.0.0.100'] |
| T8 | Servers send load reports to the controller over TCP | PASS | LOAD messages received: {'h1': 20, 'h2': 20, 'h3': 23} |
| T9 | Controller placed each new TCP connection (PacketIn -> FlowMod) | PASS | 351 connections placed: {'h1': 117, 'h2': 117, 'h3': 117} |
| T10 | Controller polls switch port statistics | PASS | 10 samples, last: {'h1': {'to_server_mbps': 0.01, 'from_server_mbps': 0.001, 'util': 0.001}, 'h2': {'to_server_mbps': 0.01, 'from_server_mbps': 0.001, 'util': 0.001}, 'h3': {'to_server_mbps': 0.011, 'from_server_mbps': 0.002, 'util': 0.001}} |

Evidence files in [results/d1/](../results/d1/): `pingall.txt`,
`curl_vip.txt`, `flows_during_load.txt` (switch rules during load),
`controller_events.jsonl`, per-request CSVs, server logs, `pox.log`.

### How to demo D1 live

```bash
sudo python3 demo.py --mode rr          # starts POX + Mininet + 3 servers
mininet> h4 curl -s 10.0.0.100:8080     # "Served by: h1", then h2, h3 ...
mininet> h4 python3 app/client.py --threads 8 --requests 10
mininet> sh ovs-ofctl dump-flows s1 | grep mod_nw
python3 experiments/watch.py            # second terminal: live controller view
```

Or rerun all D1 tests: `sudo python3 tests/d1_mininet_tests.py`.

---

## Who presents what

| Member | D1 part | Shows in the demo |
|---|---|---|
| P1 Sanjith | Mininet topology, POX controller (ARP for the VIP, PacketIn, per-connection flow rules), D1 Mininet tests | `demo.py`, `dump-flows` with `mod_nw_dst` / `mod_nw_src`, D1 test table |
| P2 Sammed | TCP server (threads, workers, queue), client load generator, traffic generator, load report protocol, socket tests | `curl` to the VIP, `client.py` with 8 threads, socket test output |
| P3 Sharvesh | Problem and architecture, socket–SDN integration (reports into the decision logic), decision logic and its unit tests | architecture slide, `watch.py` live view, unit test output |

Full roles for D1 and D2: [Team_Roles_D1_D2.pdf](Team_Roles_D1_D2.pdf).
