"""Repackage a pinned two-table publication without decoding audio or calling ASR."""

import fcntl
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.compute as pc

from ...contract import annotation_targets_schema, schema_description
from ...feature_runtime import event, file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from .publication import combine, publish_output
from .schema import result_schema


def legacy_batches(targets, results):
    """Align independently batched streams by checked sample ID and fingerprint."""
    stream = iter(results.scanner(scan_in_order=True, batch_size=4096).to_batches())
    pending = next(stream, None)
    for batch in targets.scanner(scan_in_order=True, batch_size=4096).to_batches():
        target = pa.Table.from_batches([batch])
        count = pc.sum(pc.cast(pc.equal(target["status"], "ok"), pa.int64())).as_py() or 0
        parts = []
        while count:
            if pending is None:
                raise ValueError("Missing successful result")
            n = min(count, len(pending))
            parts.append(pending.slice(0, n))
            pending = pending.slice(n) if n < len(pending) else next(stream, None)
            count -= n
        payload = pa.Table.from_batches(parts, schema=result_schema())
        yield from combine(target, payload).to_batches(max_chunksize=4096)
    if pending is not None:
        raise ValueError("Unexpected extra result")


def migrate_tables(root, source_manifest, work):
    """A new immutable run records the exact original manifest and source snapshots."""
    root, source_manifest, work = map(lambda p: Path(p).resolve(), (root, source_manifest, work))
    work.mkdir(parents=True, exist_ok=True)
    with (work / "migration.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _migrate(root, source_manifest, work)


def _migrate(root, source_manifest, work):
    original_hash = file_hash(source_manifest)
    original = json.loads(source_manifest.read_text())
    if (
        original["status"] != "complete"
        or original["task"] != "transcription"
        or original["schema_version"] != "qwen-asr-transcription-v1"
        or original["profile_id"] != digest(original["profile"])
    ):
        raise ValueError("Expected a complete, pinned v1 transcription publication")
    migration = dict(
        kind="layout_only",
        source_manifest_path=str(source_manifest.relative_to(root)),
        source_manifest_sha256=original_hash,
        source_run_id=original["run_id"],
        source_schema_version=original["schema_version"],
        target_schema_version="qwen-asr-transcription-v2",
        inference_reused=True,
    )
    plan_path = work / "migration.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        if plan["root"] != str(root) or plan["layout_migration"] != migration:
            raise ValueError("Migration inputs changed")
    else:
        plan = dict(
            root=str(root),
            run_id="tts-ann-transcription-"
            + datetime.now(BEIJING).strftime("%Y%m%dT%H%M%Sbjt")
            + "-01",
            layout_migration=migration,
        )
        if plan["run_id"] == original["run_id"]:
            raise ValueError("Migration must use a new run ID")
        write_json(plan_path, plan)
    manifest_path = root / "annotations/transcription" / plan["run_id"] / "manifest.json"
    if manifest_path.exists():
        published = json.loads(manifest_path.read_text())
        if published.get("layout_migration") != migration or published["status"] != "complete":
            raise ValueError("Conflicting published migration")
        return manifest_path
    sources = {s["alias"]: s for s in original["inputs"]}
    if len(sources) != len(original["inputs"]) or len(sources) != len(original["outputs"]):
        raise ValueError("Duplicate or missing input/output")
    outputs, seen = [], set()
    for output in original["outputs"]:
        alias = output["base_input_alias"]
        if alias in seen:
            raise ValueError("Duplicate output")
        seen.add(alias)
        source = sources[alias]
        if output["storage_kind"] != "result_table" or any(
            output[k] != source[k] for k in ("dataset_id", "release_id")
        ):
            raise ValueError("Unexpected input layout or identity")
        datasets = {}
        for kind, schema in (
            ("targets", annotation_targets_schema()),
            ("results", result_schema()),
        ):
            ref = output["tables"][kind]
            ds = lance.dataset(root / ref["table_path"], version=ref["lance_version"])
            if (
                ds.count_rows() != ref["rows"]
                or not ds.schema.equals(schema)
                or digest(schema_description(ds.schema)) != ref["schema_sha256"]
            ):
                raise ValueError("Source annotation snapshot changed")
            datasets[kind] = ds
        counts = Counter(datasets["targets"].to_table(columns=["status"])["status"].to_pylist())
        if any(
            counts[s] != output["coverage"][s] for s in ("ok", "failed", "unsupported", "skipped")
        ):
            raise ValueError("Source coverage counts changed")
        event(
            work,
            "publishing",
            dataset=alias,
            published_datasets=len(outputs),
            total_datasets=len(sources),
        )
        outputs.append(
            publish_output(
                root,
                source,
                plan["run_id"],
                work,
                lambda: legacy_batches(datasets["targets"], datasets["results"]),
                counts,
            )
        )
    if file_hash(source_manifest) != original_hash:
        raise ValueError("Original manifest changed during migration")
    manifest = dict(
        original,
        schema_version="qwen-asr-transcription-v2",
        run_id=plan["run_id"],
        outputs=outputs,
        layout_migration=migration,
        finished_at=datetime.now(BEIJING).isoformat(),
        publication_code_sha256={
            p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))
        },
    )
    write_json(manifest_path, manifest)
    event(
        work,
        "complete",
        manifest=str(manifest_path),
        counts=dict(
            Counter(
                {
                    s: sum(o["coverage"][s] for o in outputs)
                    for s in ("ok", "failed", "unsupported", "skipped")
                }
            )
        ),
    )
    return manifest_path
