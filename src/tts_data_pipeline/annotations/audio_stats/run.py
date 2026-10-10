"""CPU fragment workers, resumable checkpoints, and atomic annotation publication."""

import fcntl
import hashlib
import importlib.metadata
import json
import multiprocessing
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import soundfile as sf

from ...contract import annotation_targets_schema, schema_description
from ...feature_runtime import event, file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from .metrics import DEFINITION, process
from .schema import annotation_schema, result_schema

COLUMNS = ["sample_id", "audio", "audio_sha256", "sample_rate", "channels", "num_frames"]


def code_hashes():
    return {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


def ids_hash(ids):
    h = hashlib.sha256()
    for value in ids:
        h.update((value + "\n").encode())
    return h.hexdigest()


def prepare(root, work, output_root=None, sample_rows=0):
    root, work = Path(root).resolve(), Path(work).resolve()
    if work.exists():
        raise FileExistsError("Use a new work directory; run resumes an existing plan")
    if sample_rows < 0:
        raise ValueError("sample_rows must be nonnegative")
    definition = dict(
        DEFINITION,
        task="audio_stats",
        implementation=code_hashes(),
        dependencies={
            n: importlib.metadata.version(n) for n in ("numpy", "soundfile", "pyarrow", "pylance")
        },
        libsndfile=sf.__libsndfile_version__,
    )
    inputs, tasks = [], []
    for mp in sorted((root / "datasets").glob("*/*/manifest.json")):
        manifest = json.loads(mp.read_text())
        if manifest.get("status") != "complete" or manifest.get("artifact_kind") != "base":
            continue
        if manifest.get("validation", {}).get("unique_ids") != manifest["rows"]:
            raise ValueError(f"Base has no complete identity validation: {mp}")
        ds = lance.dataset(mp.parent / manifest["table_path"], version=manifest["lance_version"])
        if ds.count_rows() != manifest["rows"]:
            raise ValueError("Base row count differs from manifest")
        source = dict(
            alias=f"{manifest['dataset_id']}/{manifest['release_id']}",
            role="base",
            dataset_id=manifest["dataset_id"],
            release_id=manifest["release_id"],
            branch=None,
            manifest_path=str(mp.relative_to(root)),
            manifest_sha256=file_hash(mp),
            table_path=str((mp.parent / manifest["table_path"]).relative_to(root)),
            lance_version=manifest["lance_version"],
            base_rows=manifest["rows"],
        )
        fragments = ds.get_fragments()
        source_tasks = []
        if sample_rows:
            # Deterministic spread over first/middle/last fragments; no audio loaded at planning.
            fragments = [fragments[i] for i in sorted({0, len(fragments) // 2, len(fragments) - 1})]
        for fragment in fragments:
            n = fragment.count_rows()
            offsets = [max(0, (n - sample_rows) // 2)] if sample_rows else range(0, n, 8192)
            for offset in offsets:
                task = dict(
                    source=len(inputs),
                    fragment=fragment.fragment_id,
                    offset=offset,
                    rows=min(sample_rows or 8192, n - offset),
                )
                task["id"] = digest(task)
                source_tasks.append(task)
        source["target_rows"] = sum(t["rows"] for t in source_tasks)
        inputs.append(source)
        tasks.extend(source_tasks)
    if not inputs:
        raise ValueError("No complete published base datasets found")
    # Interleave datasets so that early progress covers every source.
    groups = [[t for t in tasks if t["source"] == i] for i in range(len(inputs))]
    tasks = [group[j] for j in range(max(map(len, groups))) for group in groups if j < len(group)]
    now = datetime.now(BEIJING)
    run_id = "tts-ann-audio_stats-" + now.strftime("%Y%m%dT%H%M%Sbjt") + "-01"
    plan = dict(
        root=str(root),
        output_root=str(Path(output_root or root).resolve()),
        run_id=run_id,
        created_at=now.isoformat(),
        inputs=inputs,
        tasks=tasks,
        scope="fixed_fragment_subsets" if sample_rows else "all_published_base_samples",
        profile=definition,
        profile_id=digest(definition),
    )
    work.mkdir(parents=True)
    write_json(work / "plan.json", plan)
    return plan


@lru_cache(maxsize=32)
def open_dataset(path, version):
    return lance.dataset(path, version=version)


def scan(plan, task, columns):
    source = plan["inputs"][task["source"]]
    ds = open_dataset(str(Path(plan["root"]) / source["table_path"]), source["lance_version"])
    return ds.scanner(
        fragments=[ds.get_fragment(task["fragment"])],
        columns=columns,
        offset=task["offset"],
        limit=task["rows"],
        batch_size=32,
        scan_in_order=True,
        batch_readahead=1,
        fragment_readahead=1,
    ).to_batches()


def checkpoint_path(work, task):
    return Path(work) / "checkpoints" / task["id"]


def load_checkpoint(work, task, plan_hash):
    folder = checkpoint_path(work, task)
    if not (folder / "checkpoint.json").exists():
        return None
    value = json.loads((folder / "checkpoint.json").read_text())
    if value["task"] != task or value["plan_sha256"] != plan_hash:
        raise ValueError("Checkpoint input changed")
    for name, sha in value["files"].items():
        if file_hash(folder / name) != sha:
            raise ValueError("Checkpoint file changed")
    return value


def execute_task(plan, work, task, plan_hash):
    checkpoint = load_checkpoint(work, task, plan_hash)
    if checkpoint is not None:
        return checkpoint
    start = time.monotonic()
    folder = checkpoint_path(work, task)
    folder.mkdir(parents=True, exist_ok=True)
    targets, results = [], []
    audio_bytes, audio_seconds = 0, 0.0
    for batch in scan(plan, task, COLUMNS):
        for row in batch.to_pylist():
            target, result = process(row, plan["profile_id"])
            targets.append(target)
            audio_bytes += len(row["audio"]["bytes"])
            if result is not None:
                results.append(result)
                audio_seconds += result["duration_seconds"]
    if len(targets) != task["rows"]:
        raise ValueError("Source coverage changed")
    expected = [v for b in scan(plan, task, ["sample_id"]) for v in b.column(0).to_pylist()]
    actual = [t["target_id"] for t in targets]
    if expected != actual or len(set(actual)) != len(actual):
        raise ValueError("Target identity changed or duplicated")
    if len(targets) >= 32 and not results:
        raise RuntimeError("Entire task failed decoding; investigate before continuing")
    tables = dict(
        targets=pa.Table.from_pylist(targets, schema=annotation_targets_schema()),
        results=pa.Table.from_pylist(results, schema=result_schema()),
    )
    files = {}
    for kind, table in tables.items():
        path = folder / (kind + ".parquet")
        temp = path.with_suffix(".tmp")
        pq.write_table(table, temp, compression="zstd")
        if not pq.read_table(temp).equals(table):
            raise ValueError("Checkpoint readback failed")
        temp.replace(path)
        files[path.name] = file_hash(path)
    checkpoint = dict(
        task=task,
        plan_sha256=plan_hash,
        files=files,
        counts=dict(Counter(t["status"] for t in targets)),
        errors=dict(Counter(t["error_code"] for t in targets if t["error_code"])),
        ids_sha256=ids_hash(actual),
        audio_bytes=audio_bytes,
        audio_seconds=audio_seconds,
        elapsed_seconds=time.monotonic() - start,
        length_mismatches=sum(r["num_samples_delta"] != 0 for r in results),
        format_mismatches=sum(not r["header_format_matches"] for r in results),
    )
    write_json(folder / "checkpoint.json", checkpoint)
    return checkpoint


def annotation_batches(work, tasks):
    """Combine verified legacy checkpoint pairs, aligning successful results by identity."""
    schema = annotation_schema()
    for task in tasks:
        folder = checkpoint_path(work, task)
        targets = pq.read_table(folder / "targets.parquet").combine_chunks()
        results = pq.read_table(folder / "results.parquet").combine_chunks()
        if len(targets) != task["rows"]:
            raise ValueError("Checkpoint target coverage mismatch")
        ok = pc.equal(targets["status"], "ok")
        success = targets.filter(ok)
        ids = targets["target_id"]
        if ids.null_count or pc.count_distinct(ids).as_py() != len(ids):
            raise ValueError("Duplicate or null checkpoint identity")
        if not results.select(["target_id", "input_fingerprint"]).equals(
            success.select(["target_id", "input_fingerprint"])
        ):
            raise ValueError("Checkpoint result identity or fingerprint mismatch")
        if not pc.all(pc.equal(targets["target_kind"], "sample")).as_py():
            raise ValueError("Non-sample target in audio stats")
        if not pc.all(
            pc.is_in(
                targets["status"], value_set=pa.array(["ok", "failed", "unsupported", "skipped"])
            )
        ).as_py():
            raise ValueError("Unknown checkpoint status")
        if not pc.all(pc.equal(targets["item_count"], pc.cast(ok, pa.int64()))).as_py():
            raise ValueError("Checkpoint item_count mismatch")
        if not pc.all(pc.equal(pc.is_null(targets["error_code"]), ok)).as_py():
            raise ValueError("Checkpoint error/status mismatch")
        if len(results) and (
            not pc.all(pc.equal(results["item_id"], 0)).as_py()
            or not pc.all(pc.equal(results["target_kind"], "sample")).as_py()
        ):
            raise ValueError("Invalid checkpoint result item identity")
        payload_type = schema.field("result").type
        payload = pa.StructArray.from_arrays(
            [results[field.name].combine_chunks() for field in payload_type],
            fields=list(payload_type),
        )
        index = pc.index_in(ids, value_set=results["target_id"])
        combined = pa.Table.from_arrays(
            [
                ids,
                targets["input_fingerprint"],
                targets["status"],
                targets["error_code"],
                pc.take(payload, index),
            ],
            schema=schema,
        )
        if combined["result"].null_count != len(targets) - len(success):
            raise ValueError("Result nullability does not match target status")
        yield from combined.to_batches(max_chunksize=4096)


def equal_batches(left, right):
    """Compare logical Arrow values, independent of batches and unused null-buffer bytes."""
    left, right = iter(left), iter(right)
    a, b = next(left, None), next(right, None)
    while a is not None and b is not None:
        n = min(len(a), len(b))
        if not a.slice(0, n).equals(b.slice(0, n)):
            return False
        a = a.slice(n) if len(a) > n else next(left, None)
        b = b.slice(n) if len(b) > n else next(right, None)
    return a is None and b is None


_WORKER = None


def init_worker(plan, work, plan_hash):
    global _WORKER
    pa.set_cpu_count(1)
    pa.set_io_thread_count(2)
    _WORKER = (plan, work, plan_hash)


def worker_task(task):
    plan, work, plan_hash = _WORKER
    return execute_task(plan, work, task, plan_hash)


def publish(plan, work, checkpoints):
    root = Path(plan["output_root"])
    outputs = []
    for source_index, source in enumerate(plan["inputs"]):
        selected = [t for t in plan["tasks"] if t["source"] == source_index]
        counts, errors = Counter(), Counter()
        for task in selected:
            counts.update(checkpoints[task["id"]]["counts"])
            errors.update(checkpoints[task["id"]]["errors"])
        if sum(counts.values()) != source["target_rows"]:
            raise ValueError("Publication coverage mismatch")
        final = (
            root
            / "datasets"
            / source["dataset_id"]
            / source["release_id"]
            / "annotations/audio_stats"
            / plan["run_id"]
        )
        stage = final.with_name(final.name + ".incomplete")
        receipt = Path(work) / "publications" / (source["alias"].replace("/", "__") + ".json")
        if receipt.exists():
            output = json.loads(receipt.read_text())
            if not final.exists():
                stage.rename(final)
        else:
            if final.exists():
                raise FileExistsError("Unrecognized annotation output")
            stage.mkdir(parents=True, exist_ok=True)
            schema = annotation_schema()

            def write_batches():
                # Lance 12 cannot encode sliced nullable-struct validity buffers.
                # Materialize zero-offset batches, preserving all values and nulls.
                for batch in annotation_batches(work, selected):
                    yield batch.take(pa.array(range(len(batch)), type=pa.int64()))

            ds = lance.write_dataset(
                pa.RecordBatchReader.from_batches(schema, write_batches()),
                stage / "results.lance",
                mode="overwrite",
                data_storage_version="2.2",
                max_rows_per_file=262144,
            )
            if ds.count_rows() != source["target_rows"]:
                raise ValueError("Published row count mismatch")
            if not equal_batches(
                annotation_batches(work, selected),
                ds.scanner(scan_in_order=True, batch_size=4096).to_batches(),
            ):
                raise ValueError("Published content readback mismatch")
            if source["target_rows"]:
                ds.create_scalar_index("sample_id", "BTREE")
            tag = "annotation-" + plan["run_id"]
            if tag in ds.tags.list():
                ds.tags.update(tag, ds.version)
            else:
                ds.tags.create(tag, ds.version)
            table = dict(
                table_path=str((final / "results.lance").relative_to(root)),
                branch=None,
                lance_version=ds.version,
                tag=tag,
                rows=source["target_rows"],
                schema=schema_description(schema),
                schema_sha256=digest(schema_description(schema)),
            )
            output = dict(
                base_input_alias=source["alias"],
                dataset_id=source["dataset_id"],
                release_id=source["release_id"],
                storage_kind="sample_table",
                layout_reason="one_row_per_target_including_failures",
                table=table,
                coverage=dict(
                    total_targets=source["target_rows"],
                    missing=0,
                    **{k: counts[k] for k in ("ok", "failed", "unsupported", "skipped")},
                ),
                error_counts=dict(errors),
                validation=dict(
                    readback_rows=source["target_rows"],
                    identity="all_checkpoint_ids_equal_pinned_base_ranges_base_uniqueness_verified_at_ingest",
                ),
            )
            write_json(receipt, output)
            stage.rename(final)
        if output["storage_kind"] != "sample_table":
            raise ValueError("Cannot reuse a publication with a different layout")
        table = output["table"]
        ds = lance.dataset(root / table["table_path"], version=table["lance_version"])
        if (
            ds.count_rows() != table["rows"]
            or digest(schema_description(ds.schema)) != table["schema_sha256"]
        ):
            raise ValueError("Output snapshot changed")
        outputs.append(output)
        event(
            work,
            "publishing",
            dataset=source["alias"],
            published_datasets=len(outputs),
            total_datasets=len(plan["inputs"]),
        )
    manifest = dict(
        artifact_kind="annotation",
        contract_version="v0.1",
        schema_version="audio-stats-v2",
        status="complete",
        task="audio_stats",
        target_kind="sample",
        run_id=plan["run_id"],
        profile=plan["profile"],
        profile_id=plan["profile_id"],
        scope=plan["scope"],
        inputs=plan["inputs"],
        outputs=outputs,
        finished_at=datetime.now(BEIJING).isoformat(),
        execution=json.loads((Path(work) / "execution.json").read_text()),
        publication_code_sha256=code_hashes(),
    )
    if plan["root"] != plan["output_root"]:
        manifest["experimental_input_root"] = plan["root"]
    path = root / "annotations/audio_stats" / plan["run_id"] / "manifest.json"
    if path.exists():
        raise FileExistsError("Published annotation is immutable")
    write_json(path, manifest)
    return path


def validate_implementation(work, plan):
    if plan["profile"]["implementation"] == code_hashes():
        return None
    path = Path(work) / "implementation-migration.json"
    if path.exists():
        migration = json.loads(path.read_text())
        if (
            migration["profile_id"] == plan["profile_id"]
            and migration["from"] == plan["profile"]["implementation"]
            and migration["to"] == code_hashes()
            and migration["checkpoint_schema_sha256"] == digest(schema_description(result_schema()))
        ):
            return migration
    raise ValueError("Implementation changed; resume using frozen code or audited migration")


def run(work, workers=8):
    work = Path(work).resolve()
    pa.set_cpu_count(1)
    pa.set_io_thread_count(2)
    if not 1 <= workers <= 128:
        raise ValueError("workers must be in [1,128]")
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((work / "plan.json").read_text())
        migration = validate_implementation(work, plan)
        for name, version in plan["profile"]["dependencies"].items():
            if importlib.metadata.version(name) != version:
                raise ValueError(f"Dependency changed: {name}")
        if sf.__libsndfile_version__ != plan["profile"]["libsndfile"]:
            raise ValueError("libsndfile changed")
        for source in plan["inputs"]:
            if file_hash(Path(plan["root"]) / source["manifest_path"]) != source["manifest_sha256"]:
                raise ValueError("Input manifest changed")
        destination = Path(plan["output_root"]) / "annotations/audio_stats" / plan["run_id"]
        destination.mkdir(parents=True, exist_ok=True)
        if (destination / "manifest.json").exists():
            raise FileExistsError("Run already published")
        reservation = destination / "reservation.json"
        if reservation.exists():
            if json.loads(reservation.read_text())["work"] != str(work):
                raise FileExistsError("Run ID already reserved by another work directory")
        else:
            with reservation.open("x") as stream:
                json.dump(dict(work=str(work)), stream)
        plan_hash = file_hash(work / "plan.json")
        started = time.monotonic()
        write_json(
            work / "execution.json",
            dict(
                workers=workers,
                started_at=datetime.now(BEIJING).isoformat(),
                device="cpu",
                multiprocessing="spawn",
                read_batch_rows=32,
                publication_schema_version="audio-stats-v2",
                implementation_migration=migration,
            ),
        )
        counts, errors, dataset_rows = Counter(), Counter(), Counter()
        checkpoints = {}
        total = sum(s["target_rows"] for s in plan["inputs"])
        verified, audio_seconds, audio_bytes = 0, 0.0, 0
        event(work, "starting", total_rows=total, workers=workers)
        try:
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=init_worker,
                initargs=(plan, str(work), plan_hash),
            ) as pool:
                todo = iter(plan["tasks"])
                pending = {}

                def submit():
                    task = next(todo, None)
                    if task is not None:
                        pending[pool.submit(worker_task, task)] = task

                for _ in range(workers):
                    submit()
                while pending:
                    done, _ = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                    for future in done:
                        task = pending.pop(future)
                        checkpoint = future.result()
                        checkpoints[task["id"]] = checkpoint
                        verified += task["rows"]
                        counts.update(checkpoint["counts"])
                        errors.update(checkpoint["errors"])
                        dataset_rows[plan["inputs"][task["source"]]["alias"]] += task["rows"]
                        audio_seconds += checkpoint["audio_seconds"]
                        audio_bytes += checkpoint["audio_bytes"]
                        submit()
                    elapsed = time.monotonic() - started
                    event(
                        work,
                        "running",
                        verified_rows=verified,
                        total_rows=total,
                        completed_tasks=len(checkpoints),
                        total_tasks=len(plan["tasks"]),
                        counts=dict(counts),
                        errors=dict(errors),
                        dataset_rows=dict(dataset_rows),
                        audio_seconds=audio_seconds,
                        audio_bytes=audio_bytes,
                        elapsed_seconds=round(elapsed, 2),
                        workers=workers,
                    )
            path = publish(plan, work, checkpoints)
            event(
                work,
                "complete",
                verified_rows=verified,
                total_rows=total,
                counts=dict(counts),
                errors=dict(errors),
                manifest=str(path),
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
        except Exception as exc:
            event(
                work,
                "failed",
                verified_rows=verified,
                counts=dict(counts),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
