#!/usr/bin/env bash
# Resubmits a feature job when its ACP job FAILED; the work root resumes from checkpoints.
# Usage: supervise.sh WORK_ROOT JOB_NAME [feature_pipeline.py run options...]
# Run it detached from the main checkout (setsid nohup ... &): a resubmission snapshots this
# checkout's working tree. It never resubmits a job a person stopped, suspended or deleted, a
# dirty src/scripts/configs tree, or a repeating fast failure. Logs to WORK_ROOT/supervisor.log.
set -u
WORK=$(realpath "$1"); NAME=$2; shift 2
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PY=${PYTHON_BIN:-$(awk '/^python:/ {print $2}' "$REPO/configs/acp.yaml")}
LOG=$WORK/supervisor.log
MAX_RESUBMITS=${MAX_RESUBMITS:-3}
FAST_FAIL_SECONDS=${FAST_FAIL_SECONDS:-1200}
cd "$REPO"
log() { echo "$(date '+%F %T %Z') $*" >> "$LOG"; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin).get('$1') or '')"; }
resubmits=0; fast_fails=0
log "supervisor started pid=$$ work=$WORK name=$NAME args=$*"
while true; do
  status=$("$PY" -m scripts.acp.status --work "$WORK" 2>&1) || { log "status error: $status"; sleep 120; continue; }
  state=$(echo "$status" | field state 2>/dev/null)
  case "$state" in
    SUCCEEDED)
      log "job succeeded: $status"; exit 0 ;;
    FAILED)
      ran=$(echo "$status" | python3 -c '
import json, sys, datetime
j = json.load(sys.stdin)
f = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
print(int((f(j["complete_time"]) - f(j["start_time"])).total_seconds())
      if j.get("start_time") and j.get("complete_time") else 0)')
      log "job FAILED after ${ran}s: $status"
      if (( ran < FAST_FAIL_SECONDS )); then fast_fails=$((fast_fails+1)); else fast_fails=0; fi
      if (( fast_fails >= 2 )); then log "two fast failures in a row: likely deterministic, not resubmitting"; exit 1; fi
      if (( resubmits >= MAX_RESUBMITS )); then log "resubmit limit $MAX_RESUBMITS reached"; exit 1; fi
      if [[ -n "$(git status --porcelain -- src scripts configs)" ]]; then
        log "source tree has uncommitted changes, not resubmitting"; exit 2
      fi
      if [[ -e "$WORK/hold" ]]; then log "WARNING: $WORK/hold exists; the new job will wait on it"; fi
      resubmits=$((resubmits+1))
      log "resubmitting ($resubmits/$MAX_RESUBMITS) at $(git rev-parse --short HEAD)"
      out=$("$PY" -m scripts.acp.submit --work "$WORK" --name "$NAME" --submit -- "$@" 2>&1)
      log "submit result: $(echo "$out" | grep -m1 -E '"name": "pt-' || echo "$out" | tail -3)"
      sleep 300 ;;
    "") log "unparsed status: $status"; sleep 120 ;;
    STOPPED|SUSPENDED|DELETED)
      log "job state '$state' (stopped externally or unknown), not resubmitting: $status"; exit 3 ;;
    *) sleep 120 ;;
  esac
done
