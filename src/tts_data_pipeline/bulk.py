"""Restartable parallel conversion with source identity independent of task grouping."""

import fcntl
import json
import multiprocessing
import shutil
import sqlite3
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import yaml

from .adapters import ADAPTERS, identity_scheme, libriheavy
from .contract import schema_description
from .convert import convert, file_hash
from .schema import VERSION, base_schema, digest
from .source_exclusions import record_exclusion_key, validate_record_exclusions
from .writer import (
    STORAGE_FORMAT,
    STORAGE_VERSION,
    TABLE_PATH,
    commit_fragments,
    file_batches,
    remove_uncommitted_data_files,
)
from .writer import dependencies as storage_dependencies


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def check_exclusions(root, exclusions):
    seen = set()
    for item in exclusions:
        relative = Path(item["path"])
        path = root / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("Excluded path must stay inside the source root")
        if item["path"] in seen or not item.get("reason"):
            raise ValueError("Exclusions require unique paths and a reason")
        seen.add(item["path"])
        if path.stat().st_size != item["bytes"] or file_hash(path) != item["sha256"]:
            raise ValueError(f"Excluded source changed; review policy: {item['path']}")


def source_plan(dataset, root, batch_bytes, exclusions=(), record_exclusions=()):
    validate_record_exclusions(dataset, record_exclusions)
    if dataset in {"csemotions", "libritts_r"}:
        config = "default" if dataset == "csemotions" else "all"
        selections = [(config, ADAPTERS[dataset].input_files(root, config))]
    elif dataset == "libriheavy":
        selections = [(c, libriheavy.input_files(root, c)) for c in libriheavy.CONFIGS]
    elif dataset == "mls_sidon":
        declared = yaml.safe_load((root / "paths.yaml").read_text())
        expected = {p for splits in declared.values() for paths in splits.values() for p in paths}
        actual = {str(p.relative_to(root)) for p in ADAPTERS[dataset].input_files(root)}
        if expected != actual:
            raise ValueError(
                f"MLS manifest mismatch: {len(expected - actual)} missing, "
                f"{len(actual - expected)} extra archives"
            )
        selections = [(c, ADAPTERS[dataset].input_files(root, c)) for c in sorted(declared)]
    elif dataset in ADAPTERS:
        selections = [("all", ADAPTERS[dataset].input_files(root, "all"))]
    else:
        raise ValueError("Unknown bulk dataset")
    check_exclusions(root, exclusions)
    excluded = {item["path"] for item in exclusions}
    selected = {str(path.relative_to(root)) for _, files in selections for path in files}
    if excluded - selected:
        raise ValueError("Excluded files are outside the declared source selection")
    record_paths = {item["path"] for item in record_exclusions}
    if record_paths - (selected - excluded):
        raise ValueError("Record exclusions are outside selected source files")
    pinned = {
        r["path"]: {k: r[k] for k in ("path", "bytes", "sha256", "reason")}
        for r in record_exclusions
    }
    check_exclusions(root, list(pinned.values()))
    batches = []
    for config, files in selections:
        if not files:
            raise ValueError(f"No source files for {config}")
        group, size = [], 0
        for path in files:
            if str(path.relative_to(root)) in excluded:
                continue
            stat = path.stat()
            if group and size + stat.st_size > batch_bytes:
                batches.append({"config": config, "files": group})
                group, size = [], 0
            group.append(
                {
                    "path": str(path.relative_to(root)),
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
            size += stat.st_size
        if group:
            batches.append({"config": config, "files": group})
    if not batches:
        raise ValueError("Exclusions removed all source files")
    dependency_fn = getattr(ADAPTERS[dataset], "dependency_files", lambda root, path: [])
    dependency_cache = {}
    for index, batch in enumerate(batches):
        deps = sorted(
            {dep for item in batch["files"] for dep in dependency_fn(root, root / item["path"])}
        )
        for dep in deps:
            if dep not in dependency_cache:
                dependency_cache[dep] = file_hash(dep)
        batch["semantic_dependencies"] = [
            {
                "path": str(p.relative_to(root)),
                "bytes": p.stat().st_size,
                "mtime_ns": p.stat().st_mtime_ns,
                "sha256": dependency_cache[p],
            }
            for p in deps
        ]
        batch["name"] = f"batch-{index:05d}"
        paths = {item["path"] for item in batch["files"]}
        batch["excluded_source_records"] = [r for r in record_exclusions if r["path"] in paths]
    return batches


def check_inputs(root, batch):
    for item in batch["files"] + batch.get("semantic_dependencies", []):
        stat = (root / item["path"]).stat()
        if (stat.st_size, stat.st_mtime_ns) != (item["bytes"], item["mtime_ns"]):
            raise ValueError(f"Source changed since plan: {item['path']}")
        if "sha256" in item and file_hash(root / item["path"]) != item["sha256"]:
            raise ValueError(f"Source dependency changed since plan: {item['path']}")


def state_directory(stage):
    """Control files are siblings of releases, never inside an audio dataset."""
    name = stage.name.removesuffix(".incomplete")
    return stage.parent / ".state" / name


def checkpoint_path(stage, batch):
    return state_directory(stage) / "checkpoints" / f"{batch['name']}.json"


def run_batch(job):
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    dataset, root, stage, batch, shard_bytes, deep_verify = job
    root, stage = Path(root), Path(stage)
    check_inputs(root, batch)
    checkpoint = checkpoint_path(stage, batch)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    output = state_directory(stage) / "work" / batch["name"]
    if checkpoint.exists():
        manifest = json.loads(checkpoint.read_text())
        if manifest.get("identity_scheme") != identity_scheme(dataset):
            raise ValueError("Checkpoint uses an incompatible identity scheme; migration required")
        if manifest.get("storage_format") != STORAGE_FORMAT:
            raise ValueError("Checkpoint uses an incompatible storage format")
        if manifest["status"] != "complete":
            raise ValueError(f"Invalid checkpoint: {checkpoint}")
        if manifest.get("excluded_source_records", []) != batch.get("excluded_source_records", []):
            raise ValueError("Checkpoint record exclusions differ from plan")
        for shard in manifest["shards"]:
            if file_hash(stage / shard["path"]) != shard["sha256"]:
                raise ValueError(f"Completed output changed: {stage / shard['path']}")
        return batch["name"], manifest["rows"], True
    # Orphan fragments are removed only after all workers finish and checkpoints validate.
    for partial in (output, output.with_name(output.name + ".incomplete")):
        if partial.exists():
            shutil.rmtree(partial)
    result = convert(
        dataset,
        root,
        output,
        config=batch["config"],
        shard_bytes=shard_bytes,
        deep_verify=deep_verify,
        files=[root / f["path"] for f in batch["files"]],
        data_root=stage,
        record_exclusions=batch.get("excluded_source_records", []),
    )
    check_inputs(root, batch)
    write_json(checkpoint, result)
    shutil.rmtree(output)
    return batch["name"], result["rows"], False


@contextmanager
def identity_database():
    """Rebuildable audit scratch follows TMPDIR, separate from shared output storage."""
    with tempfile.TemporaryDirectory(prefix="tts-finalize-") as folder:
        database = Path(folder) / "identity.sqlite"
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA cache_size=-65536")  # Bound this connection's page cache to 64 MiB.
            db.execute(
                "CREATE TABLE identity(sample_id TEXT PRIMARY KEY, source_key TEXT, "
                "source_config TEXT) WITHOUT ROWID"
            )
            yield db, database


def finalize(stage, plan):
    """Global ID checks use local scratch; audio was already verified in each batch."""
    started = time.monotonic()
    shards, parents, fragments, inputs = [], [], [], []
    dependencies, code_sha256 = None, None
    code_versions = {}
    rejected_records = []
    rows, duration, output_bytes, audio_bytes = 0, 0.0, 0, 0
    speakers, languages, rates = Counter(), Counter(), Counter()
    with identity_database() as (db, database):
        print(f"{plan['dataset']}: final identity audit using {database}", flush=True)
        for number, batch in enumerate(plan["batches"], start=1):
            checkpoint = checkpoint_path(stage, batch)
            manifest = json.loads(checkpoint.read_text())
            if manifest.get("identity_scheme") != identity_scheme(plan["dataset"]):
                raise ValueError(
                    "Checkpoint uses an incompatible identity scheme; migration required"
                )
            if manifest["status"] != "complete":
                raise ValueError("Unverified batch in finalization")
            parents.append(
                {
                    "task": batch["name"],
                    "sha256": file_hash(checkpoint),
                    "input_manifest_sha256": manifest["input_manifest_sha256"],
                    "code_version": digest(manifest["code_sha256"]),
                }
            )
            if manifest.get("storage_format") != STORAGE_FORMAT:
                raise ValueError("Checkpoint uses an incompatible storage format")
            allowed = plan.get("compatible_checkpoint_code_sha256")
            if allowed is not None:
                if manifest["code_sha256"] not in allowed:
                    raise ValueError("Unapproved checkpoint code version")
            elif code_sha256 is not None and manifest["code_sha256"] != code_sha256:
                raise ValueError("Checkpoint dependency/code versions differ")
            if dependencies is not None and manifest["dependencies"] != dependencies:
                raise ValueError("Checkpoint dependency/code versions differ")
            if manifest.get("excluded_source_records", []) != batch.get(
                "excluded_source_records", []
            ):
                raise ValueError("Checkpoint record exclusions differ from plan")
            rejected_records.extend(manifest.get("excluded_source_records", []))
            dependencies, code_sha256 = manifest["dependencies"], manifest["code_sha256"]
            code_versions[digest(code_sha256)] = code_sha256
            inputs.extend(manifest["inputs"])
            fragments.extend(manifest["fragments"])
            rows += manifest["rows"]
            duration += manifest["duration_seconds"]
            output_bytes += manifest["output_bytes"]
            audio_bytes += manifest["audio_bytes"]
            speakers.update(manifest["speakers"])
            languages.update(manifest["languages"])
            rates.update(manifest["sample_rates"])
            for shard in manifest["shards"]:
                path = stage / shard["path"]
                shards.append({**shard, "path": str(path.relative_to(stage))})
                for chunk in file_batches(
                    path, columns=["sample_id", "source_key", "source_config"]
                ):
                    db.executemany(
                        "INSERT INTO identity VALUES (?, ?, ?)",
                        zip(*(chunk.column(i).to_pylist() for i in range(3))),
                    )
            db.commit()
            print(
                f"{plan['dataset']}: final identity scan {number}/{len(plan['batches'])} "
                f"batches, {rows} rows, {time.monotonic() - started:.1f}s",
                flush=True,
            )
        scan_finished = time.monotonic()
        unique_ids = db.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
        if unique_ids != rows:
            raise ValueError("Global identity count mismatch")
        db.execute("CREATE INDEX by_source ON identity(source_key, source_config)")
        duplicate_keys, extra_records = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(n-1),0) FROM "
            "(SELECT COUNT(*) AS n FROM identity GROUP BY source_key HAVING COUNT(*)>1)"
        ).fetchone()
        within_config = db.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM identity GROUP BY source_key, source_config "
            "HAVING COUNT(*)>1)"
        ).fetchone()[0]
        overlap = db.execute(
            "SELECT a.source_config,b.source_config,COUNT(DISTINCT a.source_key) "
            "FROM identity a JOIN identity b ON a.source_key=b.source_key "
            "AND a.source_config<b.source_config GROUP BY a.source_config,b.source_config"
        ).fetchall()
        db.commit()
        database_bytes = database.stat().st_size
    audit_finished = time.monotonic()
    print(
        f"{plan['dataset']}: final identity checks passed; committing table and index", flush=True
    )
    if sorted(rejected_records, key=record_exclusion_key) != plan.get(
        "excluded_source_records", []
    ):
        raise ValueError("Excluded record counts/locations differ from plan")
    expected_paths = {s["path"] for s in shards}
    if len(expected_paths) != len(shards):
        raise ValueError("Repeated fragment file in checkpoints")
    # Only unreachable, uncommitted base files from interrupted attempts are removed.
    # This directory is unpublished and all worker processes have exited.
    cleanup_audit = state_directory(stage) / "cleanup.jsonl"
    removed = remove_uncommitted_data_files(
        stage / TABLE_PATH / "data", expected_paths, audit_path=cleanup_audit
    )
    print(f"{plan['dataset']}: removed {len(removed)} uncommitted data files", flush=True)
    # Include previous attempts: commit/index failures can follow a successful cleanup.
    cleanup_events = (
        [json.loads(line) for line in cleanup_audit.read_text().splitlines()]
        if cleanup_audit.exists()
        else []
    )
    ds = commit_fragments(stage / TABLE_PATH, fragments)
    if ds.count_rows() != rows:
        raise ValueError("Committed Lance row count mismatch")
    return {
        "layout": "lance-v1",
        "contract_version": VERSION,
        "schema_version": VERSION,
        "schema_sha256": digest(schema_description(base_schema())),
        "artifact_kind": "base",
        "release_id": "v0.1",
        "dataset_id": plan["dataset"],
        "inputs": sorted(inputs, key=lambda x: x["path"]),
        "dependencies": dependencies,
        "code_sha256": plan.get("code_sha256", code_sha256),
        "checkpoint_code_versions": code_versions,
        "code_migration": plan.get("code_migration"),
        "identity_scheme": identity_scheme(plan["dataset"]),
        "status": "complete",
        "dataset": plan["dataset"],
        "rows": rows,
        "duration_seconds": duration,
        "output_bytes": output_bytes,
        "audio_bytes": audio_bytes,
        "shards": shards,
        "speakers": dict(speakers),
        "languages": dict(languages),
        "sample_rates": dict(rates),
        "source_splits": {"train": rows},
        "storage_format": STORAGE_FORMAT,
        "storage_version": STORAGE_VERSION,
        "table_path": TABLE_PATH,
        "lance_version": ds.version,
        "batch_manifests": parents,
        "release_digest": digest(parents),
        "source_snapshot_scope": (
            "one source file plus declared semantic_dependencies; relative paths and SHA256"
        ),
        "excluded_source_files": plan.get("excluded_source_files", []),
        "excluded_source_records": rejected_records,
        "rejected_rows": len(rejected_records),
        "source_selection": (
            "All declared configs/splits except explicitly listed excluded sources/records"
            if plan.get("excluded_source_files") or rejected_records
            else "All declared configs/splits; source records retained separately"
        ),
        "validation": {
            "unique_ids": unique_ids,
            "lance_rows_verified": rows,
            "fully_decoded_audio": rows if plan["deep_verify"] else 0,
            "repeated_source_keys": duplicate_keys,
            "extra_source_records": extra_records,
            "repeated_keys_within_config": within_config,
            "config_overlap_by_source_key": overlap,
        },
        "finalization": {
            "cleanup": {
                "directory": f"{TABLE_PATH}/data",
                "events": cleanup_events,
                "removed_files": sum(e["action"] == "removed" for e in cleanup_events),
                "removed_bytes": sum(
                    e["bytes"] for e in cleanup_events if e["action"] == "removed"
                ),
            },
            "scratch_storage": "temporary_directory",
            "identity_database_bytes": database_bytes,
            "phase_seconds": {
                "identity_scan": round(scan_finished - started, 3),
                "identity_queries": round(audit_finished - scan_finished, 3),
                "table_commit_and_index": round(time.monotonic() - audit_finished, 3),
                "total": round(time.monotonic() - started, 3),
            },
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def bulk_convert(
    dataset,
    root,
    output,
    workers=32,
    shard_bytes=1024**3,
    batch_bytes=4 * 1024**3,
    resume=False,
    deep_verify=False,
    exclusions_path=None,
):
    root, output = root.resolve(strict=True), output.resolve()
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Output must be separate from raw corpus")
    if min(workers, shard_bytes, batch_bytes) <= 0:
        raise ValueError("Worker count and sizes must be positive")
    exclusions = []
    record_exclusions = []
    if exclusions_path is not None:
        policy = json.loads(Path(exclusions_path).read_text())
        if policy["dataset_id"] != dataset or policy["release_id"] != VERSION:
            raise ValueError("Exclusion policy targets another dataset or release")
        exclusions = sorted(policy.get("files", []), key=lambda item: item["path"])
        record_exclusions = sorted(policy.get("records", []), key=record_exclusion_key)
    validate_record_exclusions(dataset, record_exclusions)
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name(output.name + ".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if output.exists():
            raise FileExistsError("Release already exists")
        stage = output.with_name(output.name + ".incomplete")
        code_hashes = {
            str(p.relative_to(Path(__file__).parent)): file_hash(p)
            for p in sorted(Path(__file__).parent.rglob("*.py"))
        }
        if stage.exists():
            if not resume:
                raise FileExistsError("Incomplete run exists; use --resume")
            plan = json.loads((state_directory(stage) / "plan.json").read_text())
            if plan.get("identity_scheme") != identity_scheme(dataset):
                raise ValueError(
                    "Paused release uses an incompatible identity scheme; "
                    "explicit migration or a new release is required"
                )
            if plan.get("storage_format") != STORAGE_FORMAT:
                raise ValueError("Paused release uses an incompatible storage format")
            if plan.get("dependencies") != storage_dependencies():
                raise ValueError("Resume requires the same dependency versions")
            if exclusions != plan.get("excluded_source_files", []):
                raise ValueError("Resume requires the same source exclusions")
            check_exclusions(root, exclusions)
            if record_exclusions != plan.get("excluded_source_records", []):
                raise ValueError("Resume requires the same record exclusions")
            requested = (dataset, str(root), shard_bytes, batch_bytes, deep_verify, code_hashes)
            saved = tuple(
                plan[k]
                for k in (
                    "dataset",
                    "root",
                    "shard_bytes",
                    "batch_bytes",
                    "deep_verify",
                    "code_sha256",
                )
            )
            if requested != saved:
                raise ValueError("Resume requires the same source, code, and conversion settings")
        else:
            batches = source_plan(dataset, root, batch_bytes, exclusions, record_exclusions)
            plan = {
                "storage_format": STORAGE_FORMAT,
                "dependencies": storage_dependencies(),
                "layout": "lance-v1",
                "identity_scheme": identity_scheme(dataset),
                "dataset": dataset,
                "root": str(root),
                "shard_bytes": shard_bytes,
                "batch_bytes": batch_bytes,
                "deep_verify": deep_verify,
                "code_sha256": code_hashes,
                "batches": batches,
                "excluded_source_files": exclusions,
                "excluded_source_records": record_exclusions,
                "source_files": sum(len(b["files"]) for b in batches),
                "source_bytes": sum(f["bytes"] for b in batches for f in b["files"]),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            stage.mkdir()
            (stage / TABLE_PATH / "data").mkdir(parents=True)
            (state_directory(stage) / "checkpoints").mkdir(parents=True)
            write_json(state_directory(stage) / "plan.json", plan)
        status = {
            "status": "running",
            "dataset": dataset,
            "workers": workers,
            "batches_total": len(plan["batches"]),
            "batches_verified": 0,
            "rows_verified": 0,
            "source_bytes_verified": 0,
            "source_files": plan["source_files"],
            "source_bytes": plan["source_bytes"],
        }
        start = time.monotonic()
        write_json(state_directory(stage) / "status.json", status)
        print(json.dumps(status), flush=True)
        try:
            batch_sizes = {b["name"]: sum(f["bytes"] for f in b["files"]) for b in plan["batches"]}
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                futures = {
                    pool.submit(
                        run_batch, (dataset, str(root), str(stage), batch, shard_bytes, deep_verify)
                    ): batch["name"]
                    for batch in sorted(
                        plan["batches"], key=lambda b: sum(f["bytes"] for f in b["files"])
                    )
                }
                failures = []
                for future in as_completed(futures):
                    try:
                        name, rows, reused = future.result()
                        status["batches_verified"] += 1
                        status["rows_verified"] += rows
                        status["source_bytes_verified"] += batch_sizes[name]
                        print(f"Verified {name}: {rows} rows; reused={reused}", flush=True)
                    except Exception as exc:
                        failure = {"batch": futures[future], "error": str(exc)}
                        failures.append(failure)
                        print(f"FAILED {failure}", flush=True)
                    status["failed_batches"] = failures
                    status["elapsed_seconds"] = round(time.monotonic() - start, 1)
                    elapsed = max(time.monotonic() - start, 0.001)
                    status["verified_input_MB_per_second"] = round(
                        status["source_bytes_verified"] / elapsed / 1e6, 2
                    )
                    status["rows_per_second"] = round(status["rows_verified"] / elapsed, 2)
                    write_json(state_directory(stage) / "status.json", status)
            if failures:
                raise ValueError(
                    f"{len(failures)} batches failed; inspect status.json, then resume"
                )
            status["status"] = "finalizing"
            write_json(state_directory(stage) / "status.json", status)
            for batch in plan["batches"]:
                check_inputs(root, batch)
            result = finalize(stage, plan)
            write_json(stage / "manifest.json", result)
            status["status"] = "complete"
            write_json(state_directory(stage) / "status.json", status)
            shutil.rmtree(state_directory(stage) / "work", ignore_errors=True)
            stage.rename(output)
            print(f"Published {output}: {result['rows']} rows", flush=True)
            return result
        except BaseException:
            status["status"] = "failed"
            status["error"] = traceback.format_exc()
            write_json(state_directory(stage) / "status.json", status)
            raise
    finally:
        lock.close()
