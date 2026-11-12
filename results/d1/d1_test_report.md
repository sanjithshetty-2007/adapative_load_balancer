# D1 test report (generated)

Run in Mininet with the POX controller in round robin mode (`tests/d1_mininet_tests.py`).

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

Evidence files in this folder: `pingall.txt`, `curl_vip.txt`, `flows_during_load.txt`, `controller_events.jsonl`, `t4_*.csv`, `t5_*.csv`, `server_*.log`, `pox.log`.
