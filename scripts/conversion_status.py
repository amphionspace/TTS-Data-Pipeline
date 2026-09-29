"""Show running jobs and published releases, even after completed work state is removed."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

from tts_data_pipeline.manifests import read_base_manifest
from tts_data_pipeline.timestamps import parse_timestamp


def conversion_statuses(root):
    results = {}
    for status_path in sorted(root.glob("datasets/*/.state/*/status.json")):
        state = status_path.parent
        try:
            status = json.loads(status_path.read_text())
            plan = json.loads((state / "plan.json").read_text())
        except FileNotFoundError:
            # Completed work state may be removed while the published manifest remains.
            continue
        elapsed = max(
            (datetime.now(timezone.utc) - parse_timestamp(plan["started_at"])).total_seconds(),
            0.001,
        )
        if status["status"] in {"complete", "failed", "paused"}:
            elapsed = max(status.get("elapsed_seconds", elapsed), 0.001)
        results[(status["dataset"], state.name)] = {
            "dataset": status["dataset"],
            "release": state.name,
            "identity_scheme": plan.get("identity_scheme"),
            "status": status["status"],
            "status_source": "work_state",
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
        }
    for manifest_path in sorted(root.glob("datasets/*/*/manifest.json")):
        release = manifest_path.parent
        if release.name.endswith(".incomplete"):
            continue
        try:
            manifest = read_base_manifest(release)
        except FileNotFoundError:
            continue
        if manifest.get("artifact_kind") != "base" or manifest.get("status") != "complete":
            continue
        batches = len(manifest["batch_manifests"]) if "batch_manifests" in manifest else None
        input_gb = round(sum(item["bytes"] for item in manifest["inputs"]) / 1e9, 2)
        results[(manifest["dataset_id"], release.name)] = {
            "dataset": manifest["dataset_id"],
            "release": release.name,
            "identity_scheme": manifest.get("identity_scheme"),
            "status": "complete",
            "status_source": "published_manifest",
            "workers": None,
            "verified_batches": f"{batches}/{batches}" if batches is not None else None,
            "verified_rows": manifest["rows"],
            "verified_input_GB": input_gb,
            "planned_input_GB": input_gb,
            "elapsed_minutes": None,
            "average_verified_input_MB_s": None,
            "failed_batches": [],
            "finished_at": manifest["finished_at"],
            "lance_version": manifest["lance_version"],
            "finalization": manifest.get("finalization"),
        }
    return [results[key] for key in sorted(results)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/data/DATA-TTS-UNIFIED"))
    statuses = conversion_statuses(parser.parse_args().root)
    if not statuses:
        print("No conversion status files or published releases.")
    for status in statuses:
        print(json.dumps(status, ensure_ascii=False))
