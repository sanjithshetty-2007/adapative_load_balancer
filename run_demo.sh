#!/bin/bash
# One command for the whole demo. Opens one window split into three
# terminals and runs everything in them:
#
#   1 CONTROLLER  POX with the adaptive load balancer
#   2 MININET     the network; the demo commands are typed in automatically
#   3 CLIENT      the load generator inside h4, then the unit + socket tests
#
#   bash run_demo.sh
#
# About 4 minutes. At the end you are left at a normal mininet> prompt in
# terminal 2; type 'exit' there to stop everything.
# (Mouse works: click a terminal to type in it, scroll with the wheel.)

set -u
cd "$(dirname "$0")"
REPO=$(pwd)
POX_DIR=${POX_DIR:-$HOME/pox}

if ! command -v tmux >/dev/null || ! command -v mn >/dev/null || [ ! -f "$POX_DIR/pox.py" ]; then
  echo "Installing what is missing first (Mininet, Open vSwitch, POX, tmux) ..."
  bash setup.sh || exit 1
fi

echo "sudo is needed for Mininet. Type your VM password if asked."
sudo -v || exit 1

sudo tmux kill-session -t lbdemo 2>/dev/null
sudo pkill -f "[a]daptive_lb" 2>/dev/null
sudo mn -c >/dev/null 2>&1
sudo rm -f /tmp/demo_* 2>/dev/null

T="sudo tmux"
$T new-session -d -s lbdemo -x 200 -y 55 "bash $REPO/demo/t1_controller.sh $REPO $POX_DIR"
$T split-window -v -t lbdemo "python3 $REPO/demo/t2_mininet.py $REPO"
$T split-window -v -t lbdemo "bash $REPO/demo/t3_client.sh $REPO"
$T select-layout -t lbdemo even-vertical
$T set-option -t lbdemo mouse on
$T set-option -t lbdemo pane-border-status top
$T set-option -t lbdemo pane-border-format " #{pane_index}: #{pane_title} "
$T set-option -t lbdemo status off
$T select-pane -t lbdemo:0.0 -T "1 CONTROLLER (POX)"
$T select-pane -t lbdemo:0.1 -T "2 MININET"
$T select-pane -t lbdemo:0.2 -T "3 CLIENT (h4) + TESTS"
$T select-pane -t lbdemo:0.1
$T attach -t lbdemo

# After 'exit' in Mininet the session closes; make sure nothing is left.
sudo pkill -f "[a]daptive_lb" 2>/dev/null
sudo mn -c >/dev/null 2>&1
echo "Demo closed."
