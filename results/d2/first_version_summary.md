# D2 results of the FIRST version (before the fixes described in docs/D2_report.md)

Kept for the comparison in the report. Current results: summary.md.

Each value is the mean over the repetitions. Latency is the time a client waited for a full reply, including retries.

## Scenarios

| Scenario | What happens |
|---|---|
| E1 low | Equal servers, low load (8 client threads) |
| E1 medium | Equal servers, medium load (16 client threads) |
| E1 high | Equal servers, high load (32 client threads) |
| E2 unequal | Unequal servers: h1 fast (20 ms), h2 50 ms, h3 slow (150 ms) |
| E3 overload | Server overload: extra heavy traffic sent straight to h1 from 5 s to 15 s |
| E4 net load | Network load: h1's link flooded with bulk traffic from 5 s to 15 s |
| E5 crash | Server failure: h2's server process killed at 7 s, restarted at 14 s |
| E6 link down | Link failure: h3's link down at 7 s, back up at 14 s |

## Static round robin vs adaptive

| Scenario | Mode | Reps | Avg ms | p95 ms | Throughput req/s | Failed % | Retried % | h1 / h2 / h3 requests |
|---|---|---|---|---|---|---|---|---|
| E1 low | Static round robin | 3 | 82.2 | 124.7 | 97.5 | 0.00 | 0.00 | 651 / 650 / 651 |
| E1 low | Adaptive (SDN) | 3 | 88.7 | 131.4 | 90.5 | 0.00 | 0.00 | 603 / 617 / 589 |
| E1 medium | Static round robin | 3 | 86.0 | 117.9 | 188.9 | 0.00 | 0.00 | 1260 / 1259 / 1259 |
| E1 medium | Adaptive (SDN) | 3 | 101.2 | 160.7 | 163.0 | 0.00 | 0.00 | 1093 / 1083 / 1084 |
| E1 high | Static round robin | 3 | 162.9 | 245.8 | 197.3 | 0.00 | 0.00 | 1315 / 1316 / 1316 |
| E1 high | Adaptive (SDN) | 3 | 210.2 | 381.8 | 155.5 | 0.00 | 0.00 | 1014 / 1068 / 1028 |
| E2 unequal | Static round robin | 3 | 206.5 | 541.7 | 78.4 | 0.00 | 0.00 | 523 / 523 / 522 |
| E2 unequal | Adaptive (SDN) | 3 | 105.4 | 223.0 | 152.3 | 0.00 | 0.00 | 1930 / 945 / 172 |
| E3 overload | Static round robin | 3 | 152.9 | 894.4 | 105.4 | 0.00 | 0.01 | 703 / 703 / 702 |
| E3 overload | Adaptive (SDN) | 3 | 122.3 | 189.3 | 132.4 | 0.00 | 0.00 | 380 / 1132 / 1137 |
| E4 net load | Static round robin | 3 | 121.1 | 310.2 | 136.2 | 0.00 | 0.00 | 909 / 907 / 909 |
| E4 net load | Adaptive (SDN) | 3 | 123.5 | 203.7 | 131.3 | 0.00 | 0.00 | 434 / 1078 / 1115 |
| E5 crash | Static round robin | 3 | 98.9 | 270.8 | 148.2 | 2.34 | 7.99 | 1091 / 783 / 1089 |
| E5 crash | Adaptive (SDN) | 3 | 115.4 | 197.2 | 139.2 | 0.00 | 0.16 | 1084 / 605 / 1095 |
| E6 link down | Static round robin | 3 | 110.1 | 124.3 | 127.5 | 0.34 | 1.50 | 864 / 867 / 819 |
| E6 link down | Adaptive (SDN) | 3 | 116.5 | 189.3 | 139.9 | 0.00 | 0.13 | 1078 / 1106 / 613 |

## Adaptive compared with round robin

Positive = adaptive is better.

| Scenario | Avg latency | p95 latency | Throughput |
|---|---|---|---|
| E1 low | -8% | -5% | -7% |
| E1 medium | -18% | -36% | -14% |
| E1 high | -29% | -55% | -21% |
| E2 unequal | +49% | +59% | +94% |
| E3 overload | +20% | +79% | +26% |
| E4 net load | -2% | +34% | -4% |
| E5 crash | -17% | +27% | -6% |
| E6 link down | -6% | -52% | +10% |

## How fast the controller noticed each event

Measured from the controller's event log (mean over repetitions; '-' means not detected).

| Scenario | Event | Mode | Controller event | Detected after (s) | Reason logged |
|---|---|---|---|---|---|
| E3 overload | hog_start | Adaptive (SDN) | h1 overloaded | 0.59 | queue 15 > 2.0 x 4 workers |
| E3 overload | hog_start | Static round robin | h1 overloaded | 0.50 | queue 18 > 2.0 x 4 workers |
| E4 net load | flood_start | Adaptive (SDN) | h1 overloaded | 1.24 | link 94% > 80% |
| E4 net load | flood_start | Static round robin | h1 overloaded | 1.20 | link 92% > 80% |
| E5 crash | server_kill | Adaptive (SDN) | h2 server_down | 0.01 | report connection closed |
| E5 crash | server_kill | Static round robin | h2 server_down | 0.01 | report connection closed |
| E5 crash | server_restart | Adaptive (SDN) | h2 server_up | 0.07 | load reports received |
| E5 crash | server_restart | Static round robin | h2 server_up | 0.07 | load reports received |
| E6 link down | link_down | Adaptive (SDN) | h3 server_down | 0.06 | switch port down |
| E6 link down | link_down | Static round robin | h3 server_down | 0.06 | switch port down |
| E6 link down | link_up | Adaptive (SDN) | h3 server_up | 0.38 | load reports received |
| E6 link down | link_up | Static round robin | h3 server_up | 0.25 | load reports received |

In round robin mode the controller still monitors and logs these events, but the static policy ignores them; only adaptive mode acts on them (skips the server and deletes its flow rules).
