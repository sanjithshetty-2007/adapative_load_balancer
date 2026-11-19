#!/bin/bash
# Same as run_demo.sh: one window, three terminals (controller, Mininet,
# client + tests), the whole demo runs automatically.
#
#   bash demo.sh

exec bash "$(dirname "$0")/run_demo.sh" "$@"
