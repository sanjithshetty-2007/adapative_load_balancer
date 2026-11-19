#!/bin/bash
# Terminal 3 of run_demo.sh: the client inside h4, then the tests.
REPO=$1
cd "$REPO" || exit 1

echo "Waiting for the network and the servers (terminal 2) ..."
while [ ! -f /tmp/demo_ready ]; do sleep 0.5; done

echo
echo "\$ sudo bash demo/inhost.sh h4 python3 app/client.py --threads 16 --duration 90 --progress"
touch /tmp/demo_client_started
bash demo/inhost.sh h4 python3 app/client.py --threads 16 --duration 90 --progress \
    | tee /tmp/demo_client.log
touch /tmp/demo_client_done

echo
echo "\$ python3 tests/test_lb_core.py -v"
python3 tests/test_lb_core.py -v 2>&1 | tail -n 32

echo
echo "\$ python3 tests/test_sockets.py"
python3 tests/test_sockets.py 2>&1 | grep -E ' [.]{3} |^Ran |^OK|FAIL|Error'

echo
echo "Client and tests finished."
exec bash
