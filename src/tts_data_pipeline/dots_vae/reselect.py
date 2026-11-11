"""Republish audio features with a selection's targets and selected text, without GPU work."""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa

from ..contract import schema_description
from ..feature_runtime import event, file_hash, sorted_set_hash, write_json
from ..schema import digest
from ..timestamps import BEIJING
from .run import text_binding
from .storage import validate_table


def republish(root, selection_path, feature_path, work):
    root, selection_path, feature_path, work = map(Path, (root, selection_path, feature_path, work))
    root, selection_path, feature_path, work = (
        p.resolve() for p in (root, selection_path, feature_path, work)
    )
    work.mkdir(parents=True, exist_ok=False)
    event(work, "loading_selection")
    selection = json.loads(selection_path.read_text())
    old = json.loads(feature_path.read_text())
    if (
        selection["status"] != "complete"
        or old["status"] != "complete"
        or old["kind"] != "dots_vae"
    ):
        raise ValueError("Both selection and dots_vae features must be published")
    name = old["dataset_id"]
    (output,) = (o for o in selection["outputs"] if o["dataset_id"] == name)
    (base,) = (i for i in selection["inputs"] if i["dataset_id"] == name)
    (old_base,) = (i for i in old["inputs"] if i["artifact_kind"] == "base")
    if (
        old_base["manifest_sha256"] != base["manifest_sha256"]
        or old["release_id"] != output["release_id"]
        or file_hash(root / base["manifest_path"]) != base["manifest_sha256"]
    ):
        raise ValueError("Selection and features do not share the same immutable base")
    branch = lance.dataset(root / output["table_path"], version=output["base_version"])
    branch = branch.checkout_version((output["branch"], output["lance_version"]))
    chosen = (
        branch.scanner(
            columns=[
                "sample_id",
                "audio_sha256",
                "num_frames",
                "sample_rate",
                "text",
                "selected_text",
                "language",
                "selected_language",
            ],
            filter="selection_reason = 0",
            scan_in_order=True,
        )
        .to_table()
        .combine_chunks()
    )
    if len(chosen) != output["selected_rows"] or not len(chosen):
        raise ValueError("Selection count differs or is empty")
    ids = np.asarray(chosen["sample_id"].to_pylist(), dtype="S64")
    order = np.argsort(ids)
    sorted_ids = ids[order]
    if np.any(sorted_ids[1:] == sorted_ids[:-1]):
        raise ValueError("Selection has duplicate sample identities")
    release = root / "datasets" / name / old["release_id"]
    source = lance.dataset(release / old["table_path"], version=old["lance_version"])
    if source.count_rows() != old["rows"]:
        raise ValueError("Source feature snapshot differs")
    positions, selected_positions = [], []
    offset = 0
    next_report = 524288
    audio_evidence = chosen.select(["audio_sha256", "sample_rate", "num_frames"])
    event(work, "matching_features", rows_total=old["rows"])
    for batch in source.scanner(
        columns=[
            "target_id",
            "audio_sha256",
            "native_sample_rate",
            "start_frame",
            "end_frame",
            "profile_id",
        ],
        scan_in_order=True,
        batch_size=65536,
    ).to_batches():
        target_ids = np.asarray(batch["target_id"].to_pylist(), dtype="S64")
        at = np.searchsorted(sorted_ids, target_ids)
        match = (at < len(ids)) & (sorted_ids[np.minimum(at, len(ids) - 1)] == target_ids)
        indexes = np.flatnonzero(match)
        selected = order[at[match]]
        evidence = audio_evidence.take(pa.array(selected))
        records = batch.take(pa.array(indexes))
        if (
            records["audio_sha256"].to_pylist() != evidence["audio_sha256"].to_pylist()
            or records["native_sample_rate"].to_pylist() != evidence["sample_rate"].to_pylist()
            or records["end_frame"].to_pylist() != evidence["num_frames"].to_pylist()
            or any(x != 0 for x in records["start_frame"].to_pylist())
            or any(x != old["profile_id"] for x in records["profile_id"].to_pylist())
        ):
            raise ValueError("Selected feature audio identity/timeline/profile differs from base")
        positions.extend((indexes + offset).tolist())
        selected_positions.extend(selected.tolist())
        offset += len(batch)
        if offset >= next_report:
            event(work, "matching_features", rows_done=offset, rows_total=old["rows"])
            next_report += 524288
    if len(positions) != len(ids) or len(set(selected_positions)) != len(ids):
        raise ValueError("Source features do not cover selected targets exactly once")
    stamp = datetime.now(BEIJING)
    run_id = f"{name}-dots_vae-{old['profile_name']}-{stamp:%Y%m%dT%H%M%S}bjt-01"
    final = release / "features/dots_vae" / run_id
    stage = final.with_name(final.name + ".incomplete")
    stage.mkdir(parents=True, exist_ok=False)
    text_source = text_binding(root, selection, name)
    plan = {
        "selection": str(selection_path),
        "selection_sha256": file_hash(selection_path),
        "source_features": str(feature_path),
        "source_sha256": file_hash(feature_path),
        "run_id": run_id,
        "rows": len(ids),
        "stage": str(stage),
        "final": str(final),
        "implementation_sha256": file_hash(__file__),
    }
    write_json(work / "plan.json", plan)
    counts = Counter()

    def batches():
        for start in range(0, len(positions), 512):
            table = source.take(positions[start : start + 512])
            metadata = chosen.take(pa.array(selected_positions[start : start + 512])).to_pydict()
            texts = [
                selected if selected is not None else (base or "").strip()
                for selected, base in zip(metadata["selected_text"], metadata["text"], strict=True)
            ]
            if not all(texts):
                raise ValueError("Selection contains empty selected text")
            languages = [
                selected if selected is not None else base
                for selected, base in zip(
                    metadata["selected_language"], metadata["language"], strict=True
                )
            ]
            values = {"text": texts, "language": languages}
            if text_source["text_kind"] is not None:
                values["text_kind"] = [text_source["text_kind"]] * len(table)
            for key, value in values.items():
                field = table.schema.field(key)
                table = table.set_column(
                    table.schema.get_field_index(key), field, pa.array(value, type=field.type)
                )
            counts.update(table["status"].to_pylist())
            yield from table.to_batches()
            if start % 32768 == 0:
                event(work, "copying", rows_done=start + len(table), rows_total=len(ids))

    ds = lance.write_dataset(
        pa.RecordBatchReader.from_batches(source.schema, batches()),
        stage / "features.lance",
        data_storage_version="2.2",
        max_rows_per_file=65536,
        max_bytes_per_file=1024**3,
    )
    event(work, "validating", rows_total=len(ids))
    at = 0
    for batch in ds.scanner(scan_in_order=True, batch_size=512).to_batches():
        table = pa.Table.from_batches([batch])
        validate_table(table, old["profile"]["reference_rule"])
        expected = ids[np.asarray(selected_positions[at : at + len(batch)])]
        if not np.array_equal(np.asarray(batch["target_id"].to_pylist(), dtype="S64"), expected):
            raise ValueError("Published selected target order differs")
        metadata = chosen.take(pa.array(selected_positions[at : at + len(batch)])).to_pydict()
        texts = [
            selected if selected is not None else (base or "").strip()
            for selected, base in zip(metadata["selected_text"], metadata["text"], strict=True)
        ]
        languages = [
            selected if selected is not None else base
            for selected, base in zip(
                metadata["selected_language"], metadata["language"], strict=True
            )
        ]
        if batch["text"].to_pylist() != texts or batch["language"].to_pylist() != languages:
            raise ValueError("Published text/language differs from selection")
        if text_source["text_kind"] is not None and any(
            kind != text_source["text_kind"] for kind in batch["text_kind"].to_pylist()
        ):
            raise ValueError("Published text kind differs from selection")
        at += len(batch)
        if at % 32768 == 0:
            event(work, "validating", rows_done=at, rows_total=len(ids))
    if at != len(ids):
        raise ValueError("Published target count differs")
    ds.create_scalar_index("target_id", "BTREE")
    ds.create_scalar_index("feature_key", "BTREE")
    ds.tags.create(run_id, ds.version)
    manifest = {
        **old,
        "run_id": run_id,
        "rows": len(ids),
        "table_path": str((final / "features.lance").relative_to(release)),
        "lance_version": ds.version,
        "input_root": str(root),
        "schema_sha256": digest(schema_description(ds.schema)),
        "inputs": [
            {
                **old_base,
                "table_path": "samples.lance",
                "branch": output["branch"],
                "lance_version": output["lance_version"],
                "base_version": output["base_version"],
            },
            {
                "alias": "reused_features",
                "artifact_kind": "feature",
                "kind": "dots_vae",
                "dataset_id": name,
                "release_id": old["release_id"],
                "manifest_path": str(feature_path.relative_to(root)),
                "manifest_sha256": file_hash(feature_path),
                "lance_version": old["lance_version"],
            },
        ],
        "selection": {
            "mode": "selection_branch",
            "input_alias": "samples",
            "available_target_rows": output["rows"],
            "target_count": len(ids),
            "target_set_sha256": sorted_set_hash(ids),
            "filter": "selection_reason = 0",
            "manifest_path": str(selection_path.relative_to(root)),
            "manifest_sha256": file_hash(selection_path),
            "selection_id": selection["selection_id"],
        },
        "text": text_source,
        "coverage": {
            "total_targets": len(ids),
            "ok": counts["ok"],
            "failed": counts["failed"],
            "unsupported": 0,
            "skipped": 0,
            "missing": 0,
        },
        "error_counts": {},
        "validation": {
            "unique_targets": len(ids),
            "payloads_checked": counts["ok"],
            "fingerprints_checked": len(ids),
            "readback": "full published payload hashes and audio identities verified",
        },
        "execution": {"kind": "reuse_audio_features", **plan, "requested_gpus": []},
        "cleanup": {"removed_files": [], "audit_path": None},
        "finished_at": datetime.now(BEIJING).isoformat(),
    }
    if counts["failed"]:
        errors = Counter()
        for batch in ds.scanner(columns=["error_code"], filter="status = 'failed'").to_batches():
            errors.update(batch["error_code"].to_pylist())
        manifest["error_counts"] = dict(errors)
    if (
        file_hash(feature_path) != plan["source_sha256"]
        or file_hash(selection_path) != plan["selection_sha256"]
    ):
        raise ValueError("Pinned input manifest changed")
    write_json(stage / "manifest.json", manifest)
    stage.rename(final)
    event(work, "complete", manifest=str(final / "manifest.json"), rows=len(ids))
    return final / "manifest.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "selection", "features", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    republish(args.root, args.selection, args.features, args.work)
