#!/bin/bash
# One command for the quick demo in the Ubuntu VM: installs what is missing
# (Mininet, Open vSwitch, POX) and runs quick_demo.py (about 3-4 minutes).
#
#   bash demo.sh                 # runs straight through
#   bash demo.sh --step          # waits for Enter before each part
#   bash demo.sh --skip-compare  # only parts 1 and 2 (about 2 minutes)

set -u
cd "$(dirname "$0")"
POX_DIR=${POX_DIR:-$HOME/pox}

echo "sudo is needed for Mininet. Type your VM password if asked."
sudo -v || { echo "sudo failed"; exit 1; }

need=""
for p in mininet openvswitch-switch git curl iproute2; do
  dpkg -s "$p" >/dev/null 2>&1 || need="$need $p"
done
if [ -n "$need" ]; then
  echo "Installing:$need"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need
fi
sudo systemctl start openvswitch-switch 2>/dev/null || sudo service openvswitch-switch start

if [ ! -f "$POX_DIR/pox.py" ]; then
  echo "Getting the POX controller into $POX_DIR"
  git clone -q https://github.com/noxrepo/pox "$POX_DIR"
fi

sudo mn -c >/dev/null 2>&1
sudo POX_DIR="$POX_DIR" python3 quick_demo.py --pox "$POX_DIR" "$@"
status=$?
sudo chown -R "$(id -u):$(id -g)" results 2>/dev/null
exit $status
