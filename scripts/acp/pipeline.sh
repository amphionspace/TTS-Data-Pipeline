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
      # Publishing the largest dataset is memory-heavy: node RAM in kB.
      awk '/^(MemTotal|MemAvailable):/ {print "mem", $1, $2}' /proc/meminfo
      # Cumulative network bytes (rx tx) per interface: the shared filesystem is the IO path.
      awk 'NR>2 {gsub(":", "", $1); print "net", $1, $2, $10}' /proc/net/dev; } >> "$MONITOR"
  done ) &
# JIT caches publish by rename, which the node's shared-filesystem client does not apply
# reliably under concurrent writers (a Triton cache put failed with "Directory not empty").
# Keep them on node-local disk, seeded from earlier launches so workers need not recompile.
LOCAL_CACHE="/tmp/tts-features-cache-$(id -u)"
export TRITON_CACHE_DIR="$LOCAL_CACHE/triton" NUMBA_CACHE_DIR="$LOCAL_CACHE/numba" CUDA_CACHE_PATH="$LOCAL_CACHE/nv"
mkdir -p "$TRITON_CACHE_DIR" "$NUMBA_CACHE_DIR"
if [[ -d "$WORK/home/.triton/cache" ]]; then
  cp -rn "$WORK/home/.triton/cache/." "$TRITON_CACHE_DIR/" 2>/dev/null || true
  rm -rf "$TRITON_CACHE_DIR"/*/tmp.pid_*  # half-written entries of an interrupted put
fi
cd "$RUNTIME"
exec "$PYTHON_BIN" scripts/feature_pipeline.py run --work-root "$WORK" --gpus "$GPU_COUNT" "$@"
