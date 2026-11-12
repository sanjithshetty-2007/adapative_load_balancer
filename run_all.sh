#!/bin/bash
# Run every test and experiment for D1 and D2 inside the Ubuntu VM.
#
#   bash run_all.sh            # full run (D2 repeated 3 times, ~30 min)
#   REPS=1 bash run_all.sh     # quicker run (~12 min)
#
# Results:
#   results/d1/d1_test_report.md   D1 test table
#   results/d2/summary.md          D2 comparison tables
#   results/d2/graphs/*.png        D2 graphs
#   results/run_all.log            everything printed during the run

set -u
cd "$(dirname "$0")"
REPO=$(pwd)
REPS=${REPS:-3}
POX_DIR=${POX_DIR:-$HOME/pox}
mkdir -p results
LOG=$REPO/results/run_all.log
exec > >(tee "$LOG") 2>&1

step() { echo; echo "=================== $* ==================="; }

step "Start $(date)  (repo: $REPO)"
echo "sudo is needed for Mininet. Type your VM password if asked."
sudo -v || { echo "sudo failed"; exit 1; }
( while true; do sudo -n true; sleep 45; done ) 2>/dev/null &
KEEP_SUDO=$!
trap 'kill $KEEP_SUDO 2>/dev/null' EXIT

step "1. Install packages (only the missing ones)"
need=""
for p in mininet openvswitch-switch python3-matplotlib git curl iproute2; do
  dpkg -s "$p" >/dev/null 2>&1 || need="$need $p"
done
if [ -n "$need" ]; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $need
fi
sudo systemctl start openvswitch-switch 2>/dev/null || sudo service openvswitch-switch start
python3 --version; mn --version; ovs-vsctl --version | head -1

step "2. Get the POX controller"
if [ ! -f "$POX_DIR/pox.py" ]; then
  git clone https://github.com/noxrepo/pox "$POX_DIR"
fi
(cd "$POX_DIR" && git log --oneline -1)

step "3. Unit tests: controller decision logic"
python3 tests/test_lb_core.py

step "4. Socket tests on localhost: server, client, load reports"
python3 tests/test_sockets.py

step "5. D1: end-to-end tests in Mininet with POX"
sudo POX_DIR="$POX_DIR" python3 tests/d1_mininet_tests.py --pox "$POX_DIR"

step "6. D2: experiments, static round robin vs adaptive ($REPS reps)"
sudo POX_DIR="$POX_DIR" python3 experiments/run_d2.py --pox "$POX_DIR" --reps "$REPS"

sudo chown -R "$(id -u):$(id -g)" results

step "7. D2: analysis, tables and graphs"
python3 experiments/analyze.py

step "Done $(date)"
echo "D1 report: $REPO/results/d1/d1_test_report.md"
echo "D2 report: $REPO/results/d2/summary.md"
