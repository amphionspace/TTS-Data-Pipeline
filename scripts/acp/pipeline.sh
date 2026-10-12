#!/usr/bin/env bash
# Runs on the ACP node: the complete feature pipeline of one work root from a frozen runtime.
# Usage: pipeline.sh RUNTIME WORK_ROOT [feature_pipeline.py run options...]
set -euo pipefail
RUNTIME="$1"; WORK="$2"; shift 2
: "${PYTHON_BIN:?PYTHON_BIN is required}"
if [[ $EUID -eq 0 ]]; then
  # The container runs as root; write everything as the owner of the work directory so
  # shared outputs stay maintainable by that user.
  export HOME="$WORK/home"
  mkdir -p "$HOME" && chown "$(stat -c %u:%g "$WORK")" "$HOME"
  exec "$PYTHON_BIN" -c '
import os, sys
st = os.stat(sys.argv[1])
os.setgroups([]); os.setgid(st.st_gid); os.setuid(st.st_uid)
os.execv("/bin/bash", ["bash", *sys.argv[2:]])' "$WORK" "$0" "$RUNTIME" "$WORK" "$@"
fi
GPU_COUNT=$("$PYTHON_BIN" -c "import torch; print(torch.cuda.device_count())")
(( GPU_COUNT > 0 )) || { echo "No GPUs visible" >&2; exit 1; }
MONITOR="$WORK/acp-monitor/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$(dirname "$MONITOR")"
echo "uid=$(id -u) cores=$(nproc) gpus=$GPU_COUNT args=$*" | tee "$MONITOR"
# Once a minute: GPU utilization/memory and CPU load, for tuning the next launch.
( while sleep 60; do
    { date -u +%FT%TZ; cat /proc/loadavg
      nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader
      # Cumulative network bytes (rx tx) per interface: the shared filesystem is the IO path.
      awk 'NR>2 {gsub(":", "", $1); print "net", $1, $2, $10}' /proc/net/dev; } >> "$MONITOR"
  done ) &
export NUMBA_CACHE_DIR="$WORK/numba-cache"
cd "$RUNTIME"
exec "$PYTHON_BIN" scripts/feature_pipeline.py run --work-root "$WORK" --gpus "$GPU_COUNT" "$@"
