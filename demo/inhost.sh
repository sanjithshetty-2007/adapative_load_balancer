#!/bin/bash
# Run a command inside a running Mininet host, from a normal terminal.
#
#   sudo bash demo/inhost.sh h4 python3 app/client.py --threads 16 --duration 120 --progress
#
# Mininet starts each host as a shell named "mininet:<host>"; mnexec -a
# joins that host's network namespace, so the command sees the host's
# interface and IP exactly as if it were typed at the mininet> prompt.

if [ $# -lt 2 ]; then
  echo "usage: sudo bash demo/inhost.sh <host> <command...>"
  exit 1
fi

host=$1
shift
pid=$(pgrep -f "mininet:${host}\$" | head -1)

if [ -z "$pid" ]; then
  echo "Mininet host $host is not running. Start Mininet first (terminal 2)."
  exit 1
fi

exec mnexec -a "$pid" "$@"
