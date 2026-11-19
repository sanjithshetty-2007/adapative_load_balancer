#!/bin/bash
# Terminal 1 of run_demo.sh: the POX controller.
REPO=$1
POX_DIR=$2

echo "\$ cd $POX_DIR"
echo "\$ PYTHONPATH=$REPO/controller python3 pox.py openflow.of_01 --port=6633 adaptive_lb --mode=adaptive --events=/tmp/demo_events.jsonl"
echo
cd "$POX_DIR" || exit 1
PYTHONPATH="$REPO/controller" python3 pox.py openflow.of_01 --port=6633 \
    adaptive_lb --mode=adaptive --events=/tmp/demo_events.jsonl

echo
echo "Controller stopped."
exec bash
