#!/bin/bash
# Install what the project needs in the Ubuntu VM (run once):
# Mininet, Open vSwitch, matplotlib, tmux and the POX controller in ~/pox.
#
#   bash setup.sh

set -u
POX_DIR=${POX_DIR:-$HOME/pox}

sudo -v || { echo "sudo failed"; exit 1; }

need=""
for p in mininet openvswitch-switch python3-matplotlib git curl iproute2 tmux; do
  dpkg -s "$p" >/dev/null 2>&1 || need="$need $p"
done
if [ -n "$need" ]; then
  echo "Installing:$need"
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $need
fi
sudo systemctl start openvswitch-switch 2>/dev/null || sudo service openvswitch-switch start

if [ ! -f "$POX_DIR/pox.py" ]; then
  git clone https://github.com/noxrepo/pox "$POX_DIR"
fi

echo
python3 --version
mn --version
ovs-vsctl --version | head -1
echo "POX: $POX_DIR"
echo "Setup done."
