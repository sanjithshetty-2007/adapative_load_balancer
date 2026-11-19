# Demo: three terminals (about 5 minutes)

| Terminal | Runs | What you see there |
|---|---|---|
| 1 Controller | POX SDN controller (`adaptive_lb`) | servers coming UP / DOWN, OVERLOADED, flow rules deleted |
| 2 Mininet | the network, the 3 TCP servers, the failures | `mininet>` prompt: pingall, curl, flow rules |
| 3 Client | the load generator inside client host h4 | every 2 s: how many requests each server answered |

The project folder in the VM is `~/adapative_load_balancer`, and POX is in
`~/pox`. Both are set up once with `bash setup.sh`.

## 0. Once, before the demo

```bash
cd ~/adapative_load_balancer
git pull
bash setup.sh
bash demo/rehearsal.sh      # automatic dry run of all the commands below, ~3 min
```

The rehearsal must end with `ALL CHECKS PASSED`.

## Terminal 1: controller

```bash
sudo mn -c
cd ~/pox
PYTHONPATH=~/adapative_load_balancer/controller python3 pox.py openflow.of_01 --port=6633 adaptive_lb --mode=adaptive --events=/tmp/lb_events.jsonl
```

Leave it running. It prints `Listening for load reports on TCP port 9000`.

## Terminal 2: Mininet

```bash
cd ~/adapative_load_balancer
sudo mn --custom topology/topology.py --topo adaptive --link tc --mac --switch ovsk --controller remote,ip=127.0.0.1,port=6633
```

Terminal 1 now shows `Switch ... connected`. At the `mininet>` prompt:

```
net
h1 python3 app/backend_server.py --name=h1 --report-to 10.0.0.254:9000 > /tmp/h1.log 2>&1 &
h2 python3 app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/h2.log 2>&1 &
h3 python3 app/backend_server.py --name=h3 --report-to 10.0.0.254:9000 > /tmp/h3.log 2>&1 &
```

Terminal 1 shows `h1 SERVER_UP`, `h2 SERVER_UP`, `h3 SERVER_UP`.
(Write `--name=h1` with `=`: the Mininet prompt replaces a separate word
`h1` with h1's IP address.)

```
pingall
h4 curl -s 10.0.0.100:8080
h4 curl -s 10.0.0.100:8080
h4 curl -s 10.0.0.100:8080
h4 ip -c=never neigh show 10.0.0.100
sh ovs-ofctl dump-flows s1 | grep mod_nw
```

* `pingall`: 0% dropped, every host reaches every other through the switch.
* `curl` to the virtual IP 10.0.0.100: answered by a real server ("Served by: h1 / h2 / h3").
* `neigh`: the VIP's MAC is 00:00:00:00:01:00, which the controller made up.
* `dump-flows`: the rules the controller installed: `mod_nw_dst` (VIP to server)
  and `mod_nw_src` (server back to VIP).

## Terminal 3: client

```bash
cd ~/adapative_load_balancer
sudo bash demo/inhost.sh h4 python3 app/client.py --threads 16 --duration 120 --progress
```

It prints a line every 2 s, for example
`  10s | last 2s: 380 ok   0 failed | h1 127  h2 126  h3 127 | avg   82 ms`.
Equal servers get an equal share.

## Terminal 2: break things while the client runs

Type these one at a time, about 15 s apart, and point at terminals 1 and 3:

```
h7 python3 app/client.py --vip 10.0.0.1 --threads 8 --work-ms 400 --duration 15 > /tmp/hog.log 2>&1 &
```
Overload: heavy traffic sent straight to h1. Terminal 1: `h1 OVERLOADED (queue ... )`.
Terminal 3: h1's count drops, h2 and h3 take more. About 15 s later: `h1 NORMAL`.

```
h2 pkill -f "backend_server.py --name=h2"
```
Server crash. Terminal 1: `h2 SERVER_DOWN (report connection closed)` and
`h2 FLOW RULES DELETED`. Terminal 3: h2 drops to 0, almost no failed requests.

```
h2 python3 app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/h2.log 2>&1 &
```
Restart. Terminal 1: `h2 SERVER_UP`. Terminal 3: h2 gets traffic again.

```
link s1 h3 down
```
Link failure. Terminal 1: `h3 SERVER_DOWN (switch port down)`. Terminal 3: h3 drops to 0.

```
link s1 h3 up
```
Terminal 1: `h3 SERVER_UP`. Terminal 3: h3 back.

When the client finishes, terminal 3 prints the totals: requests, failed,
average and p95 latency, and how many each server answered.

## Optional: the same crash with static round robin (1 minute)

Terminal 1: press `Ctrl+C`, then

```bash
PYTHONPATH=~/adapative_load_balancer/controller python3 pox.py openflow.of_01 --port=6633 adaptive_lb --mode=rr --events=/tmp/lb_events_rr.jsonl
```

Terminal 2, about 10 s later (clears the old rules so the new controller learns the hosts):

```
sh ovs-ofctl del-flows s1
pingall
```

Terminal 3:

```bash
sudo bash demo/inhost.sh h4 python3 app/client.py --threads 16 --duration 40 --progress
```

Terminal 2, while it runs:

```
h2 pkill -f "backend_server.py --name=h2"
```

Terminal 1 still detects `h2 SERVER_DOWN`, but round robin keeps sending a
third of the connections to h2: terminal 3 shows failed requests and retries.
That is the difference adaptive makes. Restart h2 afterwards with the same
command as above.

## End

Terminal 2: `exit`. Terminal 1: `Ctrl+C`. Then `sudo mn -c`.

Optional fourth terminal for a live table of what the controller sees:
`python3 experiments/watch.py /tmp/lb_events.jsonl`.
