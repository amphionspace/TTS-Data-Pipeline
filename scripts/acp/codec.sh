#!/usr/bin/env bash
# Runs on the ACP node: process a planned codec run from a frozen runtime (copy and/or encode).
# Usage: codec.sh RUNTIME WORK WORKERS_PER_GPU DECODE_THREADS|auto MODE (all|encode)
set -euo pipefail
RUNTIME="$1"; WORK="$2"; WORKERS_PER_GPU="$3"; DECODE_THREADS="$4"; MODE="${5:-all}"
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
os.execv("/bin/bash", ["bash", *sys.argv[2:]])' "$WORK" "$0" "$@"
fi
GPU_COUNT=$("$PYTHON_BIN" -c "import torch; print(torch.cuda.device_count())")
(( GPU_COUNT > 0 )) || { echo "No GPUs visible" >&2; exit 1; }
CORES=$(nproc)
if [[ "$DECODE_THREADS" == auto ]]; then
  # Fill the dedicated node's CPUs with decode threads, keeping a few for coordination.
  DECODE_THREADS=$(( (CORES - 4) / (GPU_COUNT * WORKERS_PER_GPU) ))
  (( DECODE_THREADS > 8 )) && DECODE_THREADS=8
  (( DECODE_THREADS < 1 )) && DECODE_THREADS=1
fi
MONITOR="$WORK/acp-monitor/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$(dirname "$MONITOR")"
echo "uid=$(id -u) mode=$MODE cores=$CORES gpus=$GPU_COUNT workers_per_gpu=$WORKERS_PER_GPU decode_threads=$DECODE_THREADS" | tee "$MONITOR"
# Once a minute: GPU utilization/memory and CPU load, for tuning the next launch.
( while sleep 60; do
    { date -u +%FT%TZ; cat /proc/loadavg
      nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader; } >> "$MONITOR"
  done ) &
cd "$RUNTIME"
exec "$PYTHON_BIN" scripts/extract_codec.py run --work "$WORK" --mode "$MODE" \
  --gpus $(seq -s ' ' 0 $((GPU_COUNT - 1))) \
  --workers-per-gpu "$WORKERS_PER_GPU" --decode-threads "$DECODE_THREADS"
