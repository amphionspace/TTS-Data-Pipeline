"""Single-job conversion with sharding, immutable inputs, and full read-back checks.

Fail closed: no silent rejection and no successful publication until every row passes.
Identity sets are bounded by this job's input, not the whole bulk corpus. Bulk
workers use this runner per batch; the coordinator audits global identity on disk.
For large corpora use bulk mode rather than one unbounded direct conversion.
"""

import hashlib
import io
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from .adapters import ADAPTERS, identity_scheme
from .contract import schema_description
from .schema import (
    VERSION,
    base_schema,
    digest,
    source_file_snapshot,
    validate_record,
)
from .source_exclusions import validate_record_exclusions
from .writer import (
    STORAGE_FORMAT,
    STORAGE_VERSION,
    TABLE_PATH,
    commit_fragments,
    dependencies,
    file_batches,
    write_batches,
)


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def decode_check(row: dict) -> None:
    frames = 0
    with sf.SoundFile(io.BytesIO(row["audio"]["bytes"])) as audio:
        for block in audio.blocks(blocksize=65536, dtype="float32", always_2d=True):
            if not np.isfinite(block).all():
                raise ValueError(f"Nonfinite waveform: {row['sample_id']}")
            frames += len(block)
    if frames != row["num_frames"]:
        raise ValueError(f"Decoded frame count mismatch: {row['sample_id']}")


def convert(
    dataset: str,
    root: Path,
    output: Path,
    config: str | None = None,
    shard_bytes: int = 1024 * 1024 * 1024,
    deep_verify: bool = False,
    *,
    files: list[Path] | None = None,
    data_root: Path | None = None,
    record_exclusions=(),
) -> dict:
    root = root.resolve(strict=True)
    output = output.resolve()
    if output == root or root in output.parents or output in root.parents:
        raise ValueError("Output must be separate from the raw corpus")
    if shard_bytes <= 0:
        raise ValueError("shard_bytes must be positive")
    stage = output.with_name(output.name + ".incomplete")
    if output.exists() or stage.exists():
        raise FileExistsError("Output or incomplete run already exists; use a new output path")
    storage = data_root.resolve() if data_root is not None else stage
    if data_root is not None and (
        storage == root or storage.is_relative_to(root) or root.is_relative_to(storage)
    ):
        raise ValueError("Shared storage must be separate from raw data")
    validate_record_exclusions(dataset, record_exclusions)
    adapter = ADAPTERS[dataset]
    files = adapter.input_files(root, config) if files is None else files
    if any(not path.resolve().is_relative_to(root) for path in files):
        raise ValueError("Selected source files must be inside the raw corpus")
    if not files:
        raise ValueError("No source files selected")
    started = time.monotonic()
    dependency_fn = getattr(adapter, "dependency_files", lambda root, path: [])
    dependencies_by_file = {p: sorted(set(dependency_fn(root, p))) for p in files}
    all_files = sorted(set(files) | {d for deps in dependencies_by_file.values() for d in deps})
    if any(not p.resolve().is_relative_to(root) for p in all_files):
        raise ValueError("Semantic dependencies must be inside source root")
    source_stats = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in all_files}

    def inspect_input(path):
        stat = path.stat()
        actual_hash = file_hash(path)
        sidecar = path.with_name(path.name + ".sha256")
        if sidecar.exists() and sidecar.read_text().split()[0] != actual_hash:
            raise ValueError(f"Source checksum sidecar mismatch: {path}")
        return {
            "path": str(path.relative_to(root)),
            "bytes": stat.st_size,
            "sha256": actual_hash,
            "rows": pq.ParquetFile(path).metadata.num_rows if path.suffix == ".parquet" else None,
        }

    with ThreadPoolExecutor(max_workers=4) as executor:
        inspected = dict(zip(all_files, executor.map(inspect_input, all_files), strict=True))
    by_relative = {str(p.relative_to(root)): item for p, item in inspected.items()}
    for rejection in record_exclusions:
        item = by_relative.get(rejection["path"])
        if item is None or any(item[k] != rejection[k] for k in ("bytes", "sha256")):
            raise ValueError("Excluded record source changed or is outside selection")
    inputs = []
    for path in files:
        item = dict(inspected[path])
        deps = [dict(inspected[d]) for d in dependencies_by_file[path]]
        if deps:
            item["semantic_dependencies"] = deps
            unit = {
                "source": {k: item[k] for k in ("path", "sha256")},
                "dependencies": [{k: d[k] for k in ("path", "sha256")} for d in deps],
            }
            item["source_snapshot"] = "source-unit-v1:sha256:" + digest(unit)
        else:
            item["source_snapshot"] = source_file_snapshot(item["path"], item["sha256"])
        inputs.append(item)
    input_manifest_sha256 = digest(sorted(inputs, key=lambda item: item["path"]))
    hash_finished = time.monotonic()
    print(
        f"{dataset}: hashed {len(inputs)} input files; identity={identity_scheme(dataset)}",
        flush=True,
    )
    stage.mkdir(parents=True)
    (storage / TABLE_PATH / "data").mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "incomplete",
        "schema_version": VERSION,
        "contract_version": VERSION,
        "release_id": "v0.1",
        "artifact_kind": "base",
        "dataset_id": dataset,
        "schema_sha256": digest(schema_description(base_schema())),
        "dataset": dataset,
        "config": config,
        "identity_scheme": identity_scheme(dataset),
        "input_manifest_sha256": input_manifest_sha256,
        "source_snapshot_scope": (
            "one source file plus declared semantic_dependencies; relative paths and SHA256"
        ),
        "inputs": inputs,
        "excluded_source_files": [],
        "excluded_source_records": list(record_exclusions),
        "source_selection": "Listed inputs except explicitly listed excluded_source_records",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "shard_target_file_bytes": shard_bytes,
        "verification_level": "deep" if deep_verify else "standard",
        "storage_format": STORAGE_FORMAT,
        "storage_version": STORAGE_VERSION,
        "table_path": TABLE_PATH,
        "dependencies": dependencies(),
        "code_sha256": {
            str(p.relative_to(Path(__file__).parent)): file_hash(p)
            for p in sorted(Path(__file__).parent.rglob("*.py"))
        },
        "rejected_rows": len(record_exclusions),
        "checkpoint_code_versions": {},  # Direct jobs do not aggregate checkpoints.
        "code_migration": None,
        "finalization": None,  # Bulk identity-audit metrics do not apply to this job.
    }
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        seen = set()
        speakers, languages, rates, splits = Counter(), Counter(), Counter(), Counter()
        rows, duration, raw_bytes = 0, 0.0, 0
        expected_digest = hashlib.sha256()

        def batches():
            nonlocal rows, duration, raw_bytes
            buffer, buffer_bytes = [], 0
            for path, item in zip(files, inputs, strict=True):
                rejected = [r for r in record_exclusions if r["path"] == item["path"]]
                options = {"excluded_records": rejected} if rejected else {}
                for row in adapter.iter_records(
                    root, item["source_snapshot"], files=[path], **options
                ):
                    validate_record(row)
                    if row["sample_id"] in seen:
                        raise ValueError(f"Duplicate sample_id: {row['sample_id']}")
                    seen.add(row["sample_id"])
                    size = len(row["audio"]["bytes"])
                    buffer.append(row)
                    buffer_bytes += size
                    rows += 1
                    duration += row["duration_seconds"]
                    raw_bytes += size
                    speakers[row["speaker_id"]] += 1
                    languages[row["language"]] += 1
                    rates[str(row["sample_rate"])] += 1
                    splits[row["source_split"]] += 1
                    expected_digest.update((row["sample_id"] + row["record_revision"]).encode())
                    if buffer_bytes >= min(16 * 1024 * 1024, shard_bytes):
                        yield pa.RecordBatch.from_pylist(buffer, schema=base_schema())
                        buffer, buffer_bytes = [], 0
            if buffer:
                yield pa.RecordBatch.from_pylist(buffer, schema=base_schema())

        fragments = [
            f.to_json() for f in write_batches(batches(), storage / TABLE_PATH, shard_bytes)
        ]
        shards = []
        for fragment in fragments:
            if len(fragment["files"]) != 1:
                raise ValueError("Expected one self-contained base file per fragment")
            path = storage / TABLE_PATH / "data" / fragment["files"][0]["path"]
            shards.append(
                {
                    "path": str(path.relative_to(storage)),
                    "rows": fragment["physical_rows"],
                    "bytes": path.stat().st_size,
                    "sha256": file_hash(path),
                }
            )
        known_rows = all(item["rows"] is not None for item in inputs)
        if not rows or (
            known_rows and rows + len(record_exclusions) != sum(item["rows"] for item in inputs)
        ):
            raise ValueError("Source and output row counts differ, or no rows")
        write_finished = time.monotonic()

        restored = (
            row
            for shard in shards
            for batch in file_batches(storage / shard["path"])
            for row in batch.to_pylist()
        )
        actual_digest = hashlib.sha256()
        read_rows = 0
        for row in restored:
            validate_record(row)
            if deep_verify:
                decode_check(row)
            actual_digest.update((row["sample_id"] + row["record_revision"]).encode())
            read_rows += 1
            if read_rows % 1000 == 0:
                detail = " + full decode" if deep_verify else ""
                print(f"{dataset}: Lance read-back{detail} {read_rows}/{rows}", flush=True)
        if read_rows != rows or actual_digest.digest() != expected_digest.digest():
            raise ValueError("Lance read-back differs from source-derived records")
        readback_finished = time.monotonic()
        for path in all_files:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != source_stats[path]:
                raise ValueError(f"Input stat changed during conversion: {path}")
        if deep_verify:
            with ThreadPoolExecutor(max_workers=4) as executor:
                for index, (path, item, actual_hash) in enumerate(
                    zip(
                        all_files,
                        [inspected[p] for p in all_files],
                        executor.map(file_hash, all_files),
                        strict=True,
                    ),
                    start=1,
                ):
                    if actual_hash != item["sha256"]:
                        raise ValueError(f"Input changed during conversion: {path}")
                    if index % 10 == 0:
                        print(
                            f"{dataset}: rechecked {index}/{len(files)} source hashes", flush=True
                        )
        lance_version = None
        if data_root is None:
            ds = commit_fragments(storage / TABLE_PATH, fragments)
            if ds.count_rows() != rows:
                raise ValueError("Committed Lance row count mismatch")
            lance_version = ds.version
        manifest.update(
            fragments=fragments,
            lance_version=lance_version,
            status="complete",
            rows=rows,
            duration_seconds=duration,
            audio_bytes=raw_bytes,
            output_bytes=sum(s["bytes"] for s in shards),
            shards=shards,
            speakers=dict(speakers),
            languages=dict(languages),
            sample_rates=dict(rates),
            source_splits=dict(splits),
            validation={
                "unique_ids": len(seen),
                "lance_rows_verified": read_rows,
                "fully_decoded_audio": read_rows if deep_verify else 0,
                "source_hashes_rechecked": deep_verify,
                "source_stats_rechecked": True,
                "source_row_count_from_footer": known_rows,
                "source_records_excluded": len(record_exclusions),
                "archive_pairing_verified_to_eof": not known_rows,
                "ordered_record_digest": actual_digest.hexdigest(),
                "source_to_output_records_match": True,
            },
            elapsed_seconds=round(time.monotonic() - started, 3),
            phase_seconds={
                "source_hash": round(hash_finished - started, 3),
                "conversion": round(write_finished - hash_finished, 3),
                "readback": round(readback_finished - write_finished, 3),
                "source_recheck": round(time.monotonic() - readback_finished, 3),
            },
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        stage.rename(output)
        return manifest
    except Exception as exc:
        (stage / "FAILED.json").write_text(json.dumps({"error": str(exc)}, indent=2) + "\n")
        raise
