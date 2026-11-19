# Adaptive Load Balancer with SDN (Mininet + POX)

CN mini project #20. Multiple TCP application servers sit behind one
virtual IP. An SDN controller (POX, OpenFlow 1.0) decides, for every new
TCP connection, which server gets it, using live **server load** (reported
by the servers over a TCP socket) and **network load** (measured from switch
port counters). It detects **overloaded** and **failed** servers and reacts
by installing different flow rules. A static round robin mode is included
as the baseline, and the two are compared in repeated experiments.

## Run the demo (Ubuntu VM)

First time (downloads the project and runs the demo):

```bash
cd ~ && git clone https://github.com/sanjithshetty-2007/adapative_load_balancer.git && cd adapative_load_balancer && bash demo.sh
```

Next times (gets the latest version and runs the demo):

```bash
cd ~/adapative_load_balancer && git pull && bash demo.sh
```

`bash demo.sh` runs the whole demo in this one terminal (about 3-4 minutes):
Part 1 network and SDN controller (topology, pingall, curl to the VIP, ARP,
flow rules), Part 2 a live table while h1 is overloaded, h2 crashes and
restarts and h3's link goes down and up, Part 3 static round robin vs
adaptive. `bash demo.sh --step` waits for Enter before each part.

Three separate terminals instead (controller, Mininet, client + tests):
`bash run_demo.sh`, or type the commands yourself from [DEMO.md](DEMO.md).

## Demo in three terminals (copy each command)

Open three terminals in the VM. Run the commands in this order: terminal 1,
then terminal 2, then terminal 3. Copy one box at a time.

### Terminal 1: controller (POX)

Clean up anything left from an earlier run (old controller, old network):

```bash
sudo pkill -f pox.py; sudo fuser -k 6633/tcp 9000/tcp; sudo tmux kill-server 2>/dev/null; sudo mn -c
```

```bash
cd ~/pox && PYTHONPATH=~/adapative_load_balancer/controller python3 pox.py openflow.of_01 --port=6633 adaptive_lb --mode=adaptive --events=/tmp/lb_events.jsonl
```

Leave it running. It shows `h1 SERVER_UP`, `h1 OVERLOADED`, `h2 SERVER_DOWN`,
`h2 FLOW RULES DELETED` and so on as things happen.

### Terminal 2: Mininet

```bash
cd ~/adapative_load_balancer && sudo mn --custom topology/topology.py --topo adaptive --link tc --mac --switch ovsk --controller remote,ip=127.0.0.1,port=6633
```

Then at the `mininet>` prompt, one line at a time:

```
net
```

```
h1 python3 app/backend_server.py --name=h1 --report-to 10.0.0.254:9000 > /tmp/h1.log 2>&1 &
```

```
h2 python3 app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/h2.log 2>&1 &
```

```
h3 python3 app/backend_server.py --name=h3 --report-to 10.0.0.254:9000 > /tmp/h3.log 2>&1 &
```

```
pingall
```

```
h4 curl -s 10.0.0.100:8080
```

```
h4 ip -c=never neigh show 10.0.0.100
```

```
sh ovs-ofctl dump-flows s1 | grep mod_nw
```

### Terminal 3: client

```bash
cd ~/adapative_load_balancer && sudo bash demo/inhost.sh h4 python3 app/client.py --threads 16 --duration 120 --progress
```

Every 2 s it shows how many requests h1, h2 and h3 answered.

### Back in terminal 2, while the client runs (about 15 s apart)

Overload h1 (terminal 1: `h1 OVERLOADED`; terminal 3: h1's count drops):

```
h7 python3 app/client.py --vip 10.0.0.1 --threads 8 --work-ms 400 --duration 15 > /tmp/hog.log 2>&1 &
```

Crash h2 (terminal 1: `h2 SERVER_DOWN`, `h2 FLOW RULES DELETED`; terminal 3: h2 goes to 0):

```
h2 pkill -f "backend_server.py --name=h2"
```

Restart h2 (terminal 1: `h2 SERVER_UP`):

```
h2 python3 app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/h2.log 2>&1 &
```

Take h3's link down (terminal 1: `h3 SERVER_DOWN (switch port down)`):

```
link s1 h3 down
```

Bring it back up (terminal 1: `h3 SERVER_UP`):

```
link s1 h3 up
```

### Finish

Terminal 2: `exit`. Terminal 1: `Ctrl+C`. Then `sudo mn -c`.

| Deliverable | Report | Raw results |
|---|---|---|
| D1: design, sockets, Mininet + SDN, first tests | [docs/D1_report.md](docs/D1_report.md) | [results/d1/](results/d1/) |
| D2: full system, dynamic SDN, robustness, evaluation | [docs/D2_report.md](docs/D2_report.md) | [results/d2/](results/d2/) |

## 1. Architecture

```
                         POX controller  (controller/adaptive_lb.py)
                         +-------------------------------------------+
   load reports  ------> | report server :9000  -> Pool (lb_core.py) |
   (TCP, every 0.5 s)    | port stats every 1 s -> link utilisation  |
                         | PacketIn -> choose server -> 2 FlowMods   |
                         +---------------------+---------------------+
                                               | OpenFlow 1.0 (:6633)
     clients                                   |                 TCP servers
   h4 10.0.0.4 ---\                     +------+------+       /--- h1 10.0.0.1 :8080
   h5 10.0.0.5 ----+---- 100 Mbit/s ----|     s1      |-------+--- h2 10.0.0.2 :8080
   h6 10.0.0.6 ----+                    |  (OVS)      | 10    \--- h3 10.0.0.3 :8080
   h7 10.0.0.7 ---/                     +------+------+ Mbit/s
                                               |
                                  ctl 10.0.0.254 (root namespace, lets the
                                  servers reach the controller's :9000)

   Clients connect to the virtual IP 10.0.0.100:8080. No host owns it:
   the controller answers ARP for it and rewrites addresses in the switch.
```

What happens for one client connection:

1. The client sends ARP "who has 10.0.0.100?". The controller replies
   with the VIP MAC `00:00:00:00:01:00`.
2. The client's TCP SYN to 10.0.0.100:8080 has no matching rule, so the
   switch sends it to the controller (PacketIn).
3. The controller picks a server (round robin or adaptive, see section 4)
   and installs two exact-match rules (priority 1000, idle timeout 10 s):
   * forward: client to VIP, rewritten to the chosen server's MAC/IP
   * reverse: server to client, rewritten back to the VIP's MAC/IP
4. The SYN is sent on, and the rest of the connection stays in the
   switch. The client only ever sees the VIP.

Other traffic (ARP between hosts, load reports, direct traffic) uses
normal L2 learning rules (priority 10).

## 2. Components

| File | What it does |
|---|---|
| [app/backend_server.py](app/backend_server.py) | Multi-threaded TCP server with a fixed number of workers (queueing when busy). Sends `LOAD` reports to the controller every 0.5 s. Also answers HTTP GET for `curl`. |
| [app/client.py](app/client.py) | Load generator: many threads, one new TCP connection per request, timeout and retry. Writes a CSV per request plus a JSON summary. |
| [app/traffic_gen.py](app/traffic_gen.py) | Bulk TCP sender/sink used to flood a server link (network load). |
| [controller/adaptive_lb.py](controller/adaptive_lb.py) | POX module: ARP for the VIP, PacketIn handling, flow rules, port statistics, port status, report server, event log. |
| [controller/lb_core.py](controller/lb_core.py) | Decision logic with no POX imports (unit tested): server state, overload and failure detection, round robin and adaptive choice. |
| [topology/topology.py](topology/topology.py) | Mininet topology (3 servers, 4 clients, 1 switch, `ctl` host, TCLink bandwidths). |
| [experiments/mnlib.py](experiments/mnlib.py) | Starts POX + Mininet + servers for tests and experiments, reads the event log. |
| [tests/](tests/) | Unit tests, localhost socket tests, D1 Mininet tests. |
| [experiments/run_d2.py](experiments/run_d2.py), [analyze.py](experiments/analyze.py) | D2 scenarios (round robin vs adaptive, repeated) and the tables/graphs. |
| [demo.py](demo.py), [experiments/watch.py](experiments/watch.py) | Live demo: network + Mininet CLI, and a live view of the controller. |
| [legacy/](legacy/) | The first version (a user-space HTTP proxy, no SDN), kept for reference. |

### Socket protocols

| Link | Transport | Message |
|---|---|---|
| client to server (via VIP) | TCP :8080, one request per connection | `REQ <id> <size> [work_ms]\n` then reply `RESP <id> <server> <size>\n` + `size` bytes |
| server to controller | TCP :9000, long-lived connection | `LOAD h1 workers=4 active=6 queued=2 svc_ms=50.3 avg_ms=81.2 rps=40.0 served=812\n` every 0.5 s |
| controller to switch | OpenFlow 1.0 :6633 | PacketIn, FlowMod (with `mod_nw_dst` / `mod_nw_src`), PortStats, PortStatus, PacketOut |

`active` = connections on the server (working + waiting), `queued` =
waiting for a worker, `svc_ms` = average pure service time, `avg_ms` =
average response time including waiting (both over the last 2 s).

## 3. Monitoring and detection

| What | How | Detected within |
|---|---|---|
| Server load | `LOAD` reports over TCP every 0.5 s | 0.5 s |
| Network load | OpenFlow port statistics every 1 s; utilisation = max(tx, rx) / 10 Mbit/s | ~1.2 s |
| Overloaded server | avg response > 250 ms, or queue > 2 x workers, **and** clearly worse than the other servers; or link > 80 % busy. Back to normal below 70 % of the thresholds, or once it is no worse than its peers (hysteresis). | 0.5 to 1.2 s |
| Server process failure | the report connection closes | ~0.01 s |
| Hung / unreachable server | no report for 2 s (heartbeat) | 2 s |
| Link failure | OpenFlow PortStatus (link down) | ~0.06 s |

## 4. Balancing policies

**Static round robin (`--mode=rr`)**: h1, h2, h3, h1, ... It ignores load and
health. This is the baseline.

**Adaptive (`--mode=adaptive`)**: for each new connection, pick the healthy
server with the lowest expected completion time:

```
cost(s) = (estimated connections on s + 1) / workers(s) x svc_ms(s)
          x (1 + 4 x link_utilisation(s)^2)
          x 3 if s is overloaded
```

* **Estimated connections** start from the last report's `active`. The
  controller adds every connection it sends to s, and subtracts the ones
  s should have finished since (each busy worker finishes one per
  `svc_ms`). Every report resets the estimate to the true value. This
  keeps decisions accurate between reports, which are 0.5 s apart.
* **Failed** servers (report closed, heartbeat lost, port down) get
  nothing. Their flow rules are deleted, so clients do not wait on a
  dead path.
* **Overloaded** servers are penalised (x3) rather than cut off. They still
  get a little traffic, so their measurements stay fresh and they can
  recover.
* If every server is down, new connections are dropped until one comes
  back.

Why these choices are explained in [docs/D2_report.md](docs/D2_report.md#design-decisions),
including the problems found in the first version and how they were fixed.

## 5. How to run

Tested on Ubuntu with Mininet 2.3, Open vSwitch 3.7, POX
(`gar-experimental`) and Python 3.

### Everything at once (D1 tests + D2 experiments + graphs)

```bash
git clone <this repo> && cd adaptive-load-balancer-mininet
bash run_all.sh            # about 30 min; REPS=1 bash run_all.sh for ~12 min
```

`run_all.sh` installs any missing packages (mininet, openvswitch-switch,
python3-matplotlib), clones POX into `~/pox`, then runs:

1. `python3 tests/test_lb_core.py`: unit tests of the decision logic
2. `python3 tests/test_sockets.py`: server, client and load reports on localhost
3. `sudo python3 tests/d1_mininet_tests.py`: D1 end-to-end tests, written to `results/d1/`
4. `sudo python3 experiments/run_d2.py --reps 3`: D2 experiments, written to `results/d2/raw/`
5. `python3 experiments/analyze.py`: tables and graphs in `results/d2/`

Run only some D2 scenarios: `sudo python3 experiments/run_d2.py --only e2_hetero,e5_crash --reps 1`.

### Demo with three terminals (controller, Mininet, client)

`bash run_demo.sh` opens one window with the three terminals and runs the
whole demo and the tests automatically.
Step-by-step commands for each terminal are in [DEMO.md](DEMO.md).
`bash setup.sh` installs everything once; `bash demo/rehearsal.sh` runs
the whole demo automatically (about 3 minutes) and checks every step.

### Live demo

```bash
sudo python3 demo.py                 # adaptive; --mode rr for round robin; --unequal
python3 experiments/watch.py         # second terminal: live controller view
```

`demo.py` prints the commands to try at the `mininet>` prompt: curl the
VIP, run clients, overload h1, flood a link, kill a server, take a link
down.

### By hand (three terminals)

See [DEMO.md](DEMO.md). At the `mininet>` prompt write `--name=h1` (with
`=`): Mininet replaces a separate word that is a host name with its IP.

Controller options: `--mode=rr|adaptive --overload_ms=250 --overload_queue=2
--overload_util=0.8 --heartbeat=2 --stats_interval=1 --link_mbps=10`.

## 6. Results in short

Mean of 3 runs per case, 20 s each, 4 client hosts. Full tables and
graphs: [docs/D2_report.md](docs/D2_report.md).

| Scenario | p95 latency RR / adaptive | Throughput RR / adaptive | Failed requests RR / adaptive |
|---|---|---|---|
| Equal servers, 32 threads | 256 / 253 ms | 196 / 199 req/s | 0 / 0 |
| Unequal servers | 533 / 186 ms | 79 / 159 req/s | 0 / 0 |
| One server overloaded | 825 / 165 ms | 114 / 147 req/s | 0 / 0 |
| One server link flooded | 252 / 183 ms | 139 / 150 req/s | 0 / 0 |
| Server crash + restart | 272 / 166 ms | 142 / 158 req/s | 2.55 % / 0 |
| Server link down + up | 141 / 165 ms (p99 1899 / 280) | 116 / 157 req/s | 0.43 % / 0 |

Detection: crash 0.01 s, link down 0.05 s, server overload 0.45 s,
network overload 1.4 s. Tests: 26 unit, 9 socket and 10 D1 Mininet tests
pass ([results/d1/d1_test_report.md](results/d1/d1_test_report.md)).

## 7. Team

| Member | Role | Main files |
|---|---|---|
| P1 Sanjith | SDN controller and network: topology, POX controller (ARP for the VIP, flow rules, port stats, port status, flow deletion), test bed, D1 Mininet tests, live demo | `topology/`, `controller/adaptive_lb.py`, `experiments/mnlib.py`, `tests/d1_mininet_tests.py`, `demo.py` |
| P2 Sammed | TCP application and robustness: multi-threaded servers, load report protocol, client load generator, traffic generator, socket tests, failure/overload scenarios, live view | `app/`, `tests/test_sockets.py`, `experiments/run_d2.py`, `experiments/watch.py` |
| P3 Sharvesh | Adaptive decision logic and evaluation: server state, overload and failure detection, round robin and adaptive policies, unit tests, analysis, graphs, reports | `controller/lb_core.py`, `tests/test_lb_core.py`, `experiments/analyze.py`, `docs/` |

Roles, the problem statement and the D1 / D2 split per member are in
[docs/Team_Roles_D1_D2.pdf](docs/Team_Roles_D1_D2.pdf).
What each member used, how their part works and what each part of their code
does, for D1 and D2: [docs/Member_Work_Explained.pdf](docs/Member_Work_Explained.pdf).
