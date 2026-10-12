#!/usr/bin/env bash
# Runs on the ACP node: encode the GPU tasks of a planned codec run from a frozen runtime.
set -euo pipefail
RUNTIME="$1"; WORK="$2"; shift 2
: "${PYTHON_BIN:?PYTHON_BIN is required}"
GPUS=$("$PYTHON_BIN" -c "import torch; print(' '.join(map(str, range(torch.cuda.device_count()))))")
[[ -n "$GPUS" ]] || { echo "No GPUs visible" >&2; exit 1; }
cd "$RUNTIME"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader
# shellcheck disable=SC2086
exec "$PYTHON_BIN" scripts/extract_codec.py run --work "$WORK" --mode encode --gpus $GPUS "$@"
