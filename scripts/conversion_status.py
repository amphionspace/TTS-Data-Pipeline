"""Show live bulk progress without reading audio or changing running jobs."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, default=Path("/workspace/data/DATA-TTS-UNIFIED"))
args = parser.parse_args()
statuses = sorted(args.root.glob("datasets/*/.state/*/status.json"))
if not statuses:
    print("No conversion status files.")
for status_path in statuses:
    state = status_path.parent
    name = state.name
    status = json.loads(status_path.read_text())
    plan = json.loads((state / "plan.json").read_text())
    elapsed = max(
        (datetime.now(timezone.utc) - datetime.fromisoformat(plan["started_at"])).total_seconds(),
        0.001,
    )
    if status["status"] in {"complete", "failed", "paused"}:
        elapsed = max(status.get("elapsed_seconds", elapsed), 0.001)
    print(
        json.dumps(
            {
                "dataset": status["dataset"],
                "release": name,
                "identity_scheme": plan.get("identity_scheme"),
                "status": status["status"],
                "workers": status["workers"],
                "verified_batches": f"{status['batches_verified']}/{status['batches_total']}",
                "verified_rows": status["rows_verified"],
                "verified_input_GB": round(status["source_bytes_verified"] / 1e9, 2),
                "planned_input_GB": round(status["source_bytes"] / 1e9, 2),
                "elapsed_minutes": round(elapsed / 60, 1),
                "average_verified_input_MB_s": round(
                    status["source_bytes_verified"] / elapsed / 1e6, 2
                ),
                "failed_batches": status.get("failed_batches", []),
            },
            ensure_ascii=False,
        )
    )
