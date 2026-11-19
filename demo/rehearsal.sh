#!/bin/bash
# Automatic dry run of the 3-terminal demo (about 3 minutes, no typing).
# Uses exactly the commands from DEMO.md, then checks that the controller
# reacted as expected. Run it once before presenting:
#
#   bash demo/rehearsal.sh
#
# Output: PASS / FAIL per check. Full logs in /tmp/rh_*.

set -u
cd "$(dirname "$0")/.."
REPO=$(pwd)
POX_DIR=${POX_DIR:-$HOME/pox}
EV_A=/tmp/rh_events_adaptive.jsonl
EV_R=/tmp/rh_events_rr.jsonl
MN_SCRIPT=/tmp/rh_demo.mn

if [ ! -f "$POX_DIR/pox.py" ]; then
  echo "POX not found in $POX_DIR. Run: bash setup.sh"
  exit 1
fi

sudo -v || exit 1
sudo mn -c >/dev/null 2>&1
sudo pkill -f "[a]daptive_lb" 2>/dev/null
sudo rm -f /tmp/rh_* 2>/dev/null

POX_CMD="python3 pox.py openflow.of_01 --port=6633 adaptive_lb"

# The same Mininet commands as terminal 2 and 3 in DEMO.md.
cat > "$MN_SCRIPT" <<EOF
h1 python3 $REPO/app/backend_server.py --name=h1 --report-to 10.0.0.254:9000 > /tmp/rh_h1.log 2>&1 &
h2 python3 $REPO/app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/rh_h2.log 2>&1 &
h3 python3 $REPO/app/backend_server.py --name=h3 --report-to 10.0.0.254:9000 > /tmp/rh_h3.log 2>&1 &
sh sleep 5
pingall
h4 curl -s 10.0.0.100:8080
h4 curl -s 10.0.0.100:8080
h4 curl -s 10.0.0.100:8080
h4 ip -c=never neigh show 10.0.0.100
sh ovs-ofctl dump-flows s1 | grep mod_nw | head -4
sh bash $REPO/demo/inhost.sh h4 python3 $REPO/app/client.py --threads 16 --duration 60 --progress > /tmp/rh_client_adaptive.log 2>&1 &
sh sleep 8
h7 python3 $REPO/app/client.py --vip 10.0.0.1 --threads 8 --work-ms 400 --duration 12 > /tmp/rh_hog.log 2>&1 &
sh sleep 14
h2 pkill -f "backend_server.py --name=h2"
sh sleep 8
h2 python3 $REPO/app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/rh_h2b.log 2>&1 &
sh sleep 6
link s1 h3 down
sh sleep 8
link s1 h3 up
sh sleep 22
sh cat /tmp/rh_client_adaptive.log
sh pkill -f "[a]daptive_lb"
sh sleep 2
sh cd $POX_DIR && PYTHONPATH=$REPO/controller setsid nohup $POX_CMD --mode=rr --events=$EV_R > /tmp/rh_pox_rr.log 2>&1 &
sh sleep 10
sh ovs-ofctl del-flows s1
pingall
sh sleep 3
sh bash $REPO/demo/inhost.sh h4 python3 $REPO/app/client.py --threads 16 --duration 30 --progress > /tmp/rh_client_rr.log 2>&1 &
sh sleep 8
h2 pkill -f "backend_server.py --name=h2"
sh sleep 8
h2 python3 $REPO/app/backend_server.py --name=h2 --report-to 10.0.0.254:9000 > /tmp/rh_h2c.log 2>&1 &
sh sleep 18
sh cat /tmp/rh_client_rr.log
EOF

echo "=== Starting POX (adaptive) ==="
(cd "$POX_DIR" && PYTHONPATH="$REPO/controller" $POX_CMD --mode=adaptive --events=$EV_A > /tmp/rh_pox_adaptive.log 2>&1) &
sleep 4

echo "=== Running the demo commands in Mininet (about 3 minutes) ==="
sudo mn --custom topology/topology.py --topo adaptive --link tc --mac --switch ovsk \
        --controller remote,ip=127.0.0.1,port=6633 --test none --post "$MN_SCRIPT" \
        2>&1 | tee /tmp/rh_mininet.log

sudo pkill -f "[a]daptive_lb" 2>/dev/null
sudo mn -c >/dev/null 2>&1

echo
echo "=== Checks ==="
python3 - "$EV_A" "$EV_R" <<'PY'
import json, re, sys

def events(path):
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out

def has(evs, ev, server, reason=""):
    return [e for e in evs if e.get("ev") == ev and e.get("server") == server
            and reason in e.get("reason", "")]

def client(path):
    try:
        text = open(path).read()
    except OSError:
        return None
    m = re.search(r"(\d+) requests, (\d+) ok, (\d+) failed", text)
    return tuple(int(x) for x in m.groups()) if m else None

a, r = events(sys.argv[1]), events(sys.argv[2])
mn = open("/tmp/rh_mininet.log", errors="replace").read()
results = []

def check(name, ok, detail=""):
    results.append(ok)
    print("  %s  %s %s" % ("PASS" if ok else "FAIL", name, detail))

check("POX started and the switch connected", bool([e for e in a if e.get("ev") == "switch_up"]))
check("h1, h2, h3 reported to the controller",
      all(has(a, "server_up", s) for s in ("h1", "h2", "h3")))
check("pingall: 0% dropped", "0% dropped" in mn)
check("curl to the VIP was answered by a server",
      len(re.findall(r"^Served by: h\d\s*$", mn, re.M)) >= 3)
check("ARP for the VIP answered with 00:00:00:00:01:00", "00:00:00:00:01:00" in mn.lower())
check("rewrite rules seen in the switch", "mod_nw_dst" in mn)
check("h1 detected as OVERLOADED", bool(has(a, "overloaded", "h1")))
check("h2 crash detected (report connection closed)",
      bool(has(a, "server_down", "h2", "report connection closed")))
check("h2 flow rules deleted", bool(has(a, "flows_deleted", "h2")))
check("h2 back UP after restart", len(has(a, "server_up", "h2")) >= 2)
check("h3 link down detected (switch port down)",
      bool(has(a, "server_down", "h3", "switch port down")))
check("h3 back UP after link up", len(has(a, "server_up", "h3")) >= 2)
ca = client("/tmp/rh_client_adaptive.log")
check("adaptive client run finished", ca is not None and ca[1] > 0,
      "(%d requests, %d ok, %d failed)" % ca if ca else "")
check("round robin controller ran and saw h2 go down",
      bool(has(r, "server_down", "h2")))
cr = client("/tmp/rh_client_rr.log")
check("round robin client run finished", cr is not None and cr[1] > 0,
      "(%d requests, %d ok, %d failed)" % cr if cr else "")

print()
print("ALL CHECKS PASSED - the demo commands work." if all(results)
      else "Some checks failed. Logs: /tmp/rh_pox_adaptive.log, /tmp/rh_mininet.log, /tmp/rh_client_*.log")
PY
