#!/usr/bin/env bash
# Runs on the ACP node: one CPU-only script of the frozen runtime, as the work directory owner.
# Usage: cpu_job.sh RUNTIME WORK SCRIPT [script arguments...]
set -euo pipefail
RUNTIME="$1"; WORK="$2"; shift 2
: "${PYTHON_BIN:?PYTHON_BIN is required}"
if [[ $EUID -eq 0 ]]; then
  export HOME="$WORK/home"
  mkdir -p "$HOME" && chown "$(stat -c %u:%g "$WORK")" "$HOME"
  exec "$PYTHON_BIN" -c '
import os, sys
st = os.stat(sys.argv[1])
os.setgroups([]); os.setgid(st.st_gid); os.setuid(st.st_uid)
os.execv("/bin/bash", ["bash", *sys.argv[2:]])' "$WORK" "$0" "$RUNTIME" "$WORK" "$@"
fi
MONITOR="$WORK/acp-monitor/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$(dirname "$MONITOR")"
echo "uid=$(id -u) cores=$(nproc) args=$*" | tee "$MONITOR"
( while sleep 60; do
    { date -u +%FT%TZ; cat /proc/loadavg
      awk 'NR>2 {gsub(":", "", $1); print "net", $1, $2, $10}' /proc/net/dev; } >> "$MONITOR"
  done ) &
cd "$RUNTIME"
exec "$PYTHON_BIN" "$@"
