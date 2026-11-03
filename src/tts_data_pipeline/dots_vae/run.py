"""All samples of one base release -> GPU workers -> resumable, audited dots_vae table.

Targets are every row of the pinned base snapshot (`all_samples`). Tasks are fixed slices of
its fragments; each worker writes an uncommitted fragment plus a hashed checkpoint, and only
the coordinator commits and publishes.
"""

import fcntl
import json
import multiprocessing
import queue
import random
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
from lance.file import LanceFileReader
from lance.fragment import FragmentMetadata, write_fragments

from ..contract import dots_vae_schema, schema_description
from ..feature_contract import make_feature_run_id, validate_feature_coverage
from ..feature_runtime import (
    event,
    file_hash,
    ordered_hash,
    sorted_set_hash,
    verify_checkpoint,
    write_json,
)
from ..manifests import read_base_manifest
from ..schema import digest
from ..timestamps import BEIJING
from ..writer import remove_uncommitted_data_files
from .profile import KIND, PROFILE_NAME, profile
from .storage import make_table, validate_table

INPUT_COLUMNS = [
    "sample_id",
    "audio",
    "audio_sha256",
    "sample_rate",
    "channels",
    "num_frames",
    "parent_sample_id",
    "text",
    "text_kind",
    "language",
]


def open_base(d):
    return lance.dataset(d["source"]["table_path"], version=d["source"]["lance_version"])


def prepare(root, dataset, work, model_root, *, release="v0.1", task_size=1024, output_root=None):
    work = Path(work).resolve()
    release_dir = Path(root) / "datasets" / dataset / release
    base = read_base_manifest(release_dir)
    if base["status"] != "complete":
        raise ValueError("Base release is not complete")
    definition = profile(model_root)
    source = dict(
        dataset_id=dataset,
        release_id=release,
        table_path=str(release_dir / base["table_path"]),
        branch=None,
        lance_version=base["lance_version"],
        rows=base["rows"],
        manifest_path=str(Path("datasets") / dataset / release / "manifest.json"),
        manifest_sha256=file_hash(release_dir / "manifest.json"),
    )
    ds = lance.dataset(source["table_path"], version=source["lance_version"])
    if ds.count_rows() != base["rows"]:
        raise ValueError("Base snapshot row count differs from its manifest")
    tasks, ids = [], []
    for fragment in ds.get_fragments():
        fragment_ids = (
            ds.scanner(fragments=[fragment], columns=["sample_id"], scan_in_order=True)
            .to_table()["sample_id"]
            .to_pylist()
        )
        ids.append(np.asarray(fragment_ids, dtype="S64"))
        for offset in range(0, len(fragment_ids), task_size):
            selected = fragment_ids[offset : offset + task_size]
            task = dict(
                dataset=dataset,
                fragment=fragment.fragment_id,
                offset=offset,
                rows=len(selected),
                ordered_ids_sha256=ordered_hash(selected),
            )
            task["id"] = digest(task)
            tasks.append(task)
    ids = np.concatenate(ids)
    if len(ids) != base["rows"]:
        raise ValueError("Base fragments do not cover the manifest rows")
    created = datetime.now(BEIJING).isoformat()
    run_id = make_feature_run_id(dataset, KIND, PROFILE_NAME, created)
    output_root = Path(output_root or root).resolve()
    final = output_root / "datasets" / dataset / release / "features" / KIND / run_id
    p = dict(
        root=str(Path(root).resolve()),
        output_root=str(output_root),
        work=str(work),
        model_root=str(Path(model_root).resolve()),
        created_at=created,
        profile=definition,
        profile_id=digest(definition),
        profile_name=PROFILE_NAME,
        execution_code_sha256=file_hash(__file__),
        datasets=[
            dict(
                source=source,
                target_count=len(ids),
                target_set_sha256=sorted_set_hash(ids),
                tasks=tasks,
                run_id=run_id,
                final=str(final),
                stage=str(final.with_name(run_id + ".incomplete")),
                state=str(work / "state" / dataset),
            )
        ],
    )
    work.mkdir(parents=True, exist_ok=False)
    write_json(work / "plan.json", p)
    validate_plan(p)
    event(work, "planned", rows_total=len(ids), tasks=len(tasks))
    return p


def validate_plan(p):
    if (
        file_hash(__file__) != p["execution_code_sha256"]
        or profile(p["model_root"]) != p["profile"]
    ):
        raise ValueError("Pinned implementation, model or numerical profile changed")
    for d in p["datasets"]:
        manifest = Path(p["root"]) / d["source"]["manifest_path"]
        if file_hash(manifest) != d["source"]["manifest_sha256"]:
            raise ValueError("Base manifest changed")


def read_task(d, task):
    started = time.perf_counter()
    ds = open_base(d)
    rows = (
        ds.scanner(
            fragments=[ds.get_fragment(task["fragment"])],
            offset=task["offset"],
            limit=task["rows"],
            columns=INPUT_COLUMNS,
            scan_in_order=True,
            batch_readahead=1,
            fragment_readahead=1,
        )
        .to_table()
        .to_pylist()
    )
    ids = [r["sample_id"] for r in rows]
    if len(rows) != task["rows"] or ordered_hash(ids) != task["ordered_ids_sha256"]:
        raise ValueError("Task target set changed")
    if any(r["parent_sample_id"] is not None for r in rows):
        raise ValueError("Segment views are not supported")
    return dict(rows=rows, ids=ids, started=started)


def write_task(p, d, task, prepared, values, hardware):
    rows, ids = prepared["rows"], prepared["ids"]
    table = make_table(rows, values, p["profile"], p["profile_id"])
    if table["target_id"].to_pylist() != ids:
        raise ValueError("Task table order differs from the pinned targets")
    validate_table(table)
    destination = Path(d["stage"]) / "features.lance"
    fragments = write_fragments(
        table,
        destination,
        schema=table.schema,
        mode="create",
        data_storage_version="2.2",
        max_bytes_per_file=1024**3,
    )
    files, batches = [], []
    for fragment in fragments:
        for data_file in fragment.files:
            path = destination / "data" / data_file.path
            files.append(dict(name=path.name, bytes=path.stat().st_size, sha256=file_hash(path)))
            batches.extend(LanceFileReader(str(path)).read_all(batch_size=256).to_batches())
    back = pa.Table.from_batches(batches, schema=table.schema)
    if not back.equals(table):
        raise ValueError("dots_vae fragment readback differs")
    checkpoint = dict(
        task=task,
        profile_id=p["profile_id"],
        fragments=[f.to_json() for f in fragments],
        files=files,
        rows=len(rows),
        audio_seconds=sum(r["num_frames"] / r["sample_rate"] for r in rows),
        latent_frames=int(sum(v[1].shape[0] for v in values if v[1] is not None)),
        wall_seconds=time.perf_counter() - prepared["started"],
        payloads_validated=True,
        hardware=dict(hardware),
        error_counts=dict(Counter(v[4] for v in values if v[4])),
    )
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", checkpoint)
    return checkpoint


def next_job(jobs):
    while True:
        try:
            return jobs.get(timeout=30)
        except queue.Empty:
            parent = multiprocessing.parent_process()
            if parent is not None and not parent.is_alive():
                raise RuntimeError("Coordinator exited; releasing the GPU") from None


def worker(gpu, p, jobs, results, cpu_threads):
    try:
        import torch

        from .model import Encoder

        torch.set_num_threads(2)
        pa.set_cpu_count(2)
        pa.set_io_thread_count(2)
        encoder = Encoder(p["model_root"], f"cuda:{gpu}")
        sources = {d["source"]["dataset_id"]: d for d in p["datasets"]}
        with (
            ThreadPoolExecutor(max_workers=cpu_threads) as pool,
            ThreadPoolExecutor(max_workers=1) as reader,
            ThreadPoolExecutor(max_workers=1) as writer,
        ):
            item = next_job(jobs)
            future = None if item is None else reader.submit(read_task, sources[item[0]], item[1])
            written = None
            while item is not None:
                prepared = future.result()
                following = next_job(jobs)
                if following is not None:
                    future = reader.submit(read_task, sources[following[0]], following[1])
                values = encoder.extract(prepared["rows"], pool)
                if written is not None:
                    results.put(("checkpoint", written.result()))
                written = writer.submit(
                    write_task, p, sources[item[0]], item[1], prepared, values, encoder.hardware
                )
                item = following
            if written is not None:
                results.put(("checkpoint", written.result()))
        results.put(("done", gpu))
    except BaseException:
        results.put(("error", dict(gpu=gpu, traceback=traceback.format_exc())))
        raise


def published_manifest(p, d):
    final = Path(d["final"])
    manifest = json.loads((final / "manifest.json").read_text())
    validate_feature_coverage(manifest)
    ds = lance.dataset(final / "features.lance", version=manifest["lance_version"])
    if (
        manifest["profile_id"] != p["profile_id"]
        or manifest["run_id"] != d["run_id"]
        or manifest["selection"]["target_set_sha256"] != d["target_set_sha256"]
        or ds.count_rows() != d["target_count"]
    ):
        raise ValueError("Published output differs from plan")
    return manifest


def finalize(p, d):
    stage, final = Path(d["stage"]), Path(d["final"])
    if final.exists():
        return published_manifest(p, d)
    with ThreadPoolExecutor(max_workers=8) as pool:
        checkpoints = list(pool.map(lambda t: verify_checkpoint(d, t), d["tasks"]))
    if any(c is None for c in checkpoints):
        raise ValueError("Incomplete target coverage")
    if any(c["profile_id"] != p["profile_id"] for c in checkpoints):
        raise ValueError("Checkpoint profile changed")
    fragments = [
        FragmentMetadata.from_json(json.dumps(f)) for c in checkpoints for f in c["fragments"]
    ]
    destination = stage / "features.lance"
    previous = lance.dataset(destination).version if (destination / "_versions").exists() else 0
    schema = dots_vae_schema()
    ds = lance.LanceDataset.commit(
        destination, lance.LanceOperation.Overwrite(schema, fragments), read_version=previous
    )
    ids = np.empty(d["target_count"], dtype="S64")
    count, status = 0, Counter()
    for batch in ds.to_batches(columns=["target_id", "profile_id", "status"], batch_size=16384):
        for row in batch.to_pylist():
            if row["profile_id"] != p["profile_id"]:
                raise ValueError("Mixed profiles")
            if count >= len(ids):
                raise ValueError("Unexpected extra targets")
            ids[count] = row["target_id"]
            status[row["status"]] += 1
            count += 1
    if count != d["target_count"] or sorted_set_hash(ids) != d["target_set_sha256"]:
        raise ValueError("Published target set disagrees")
    ds.create_scalar_index("target_id", "BTREE")
    ds.create_scalar_index("feature_key", "BTREE")
    if d["run_id"] in ds.tags.list():
        ds.tags.delete(d["run_id"])
    ds.tags.create(d["run_id"], ds.version)
    referenced = set()
    for version in ds.versions():
        for fragment in ds.checkout_version(version["version"]).get_fragments():
            referenced.update(f.path for f in fragment.metadata.files)
    removed = remove_uncommitted_data_files(
        destination / "data", referenced, audit_path=stage / "cleanup.jsonl"
    )
    source = d["source"]
    manifest = {
        "contract_version": "v0.1",
        "artifact_kind": "feature",
        "kind": KIND,
        "status": "complete",
        "dataset_id": source["dataset_id"],
        "release_id": source["release_id"],
        "run_id": d["run_id"],
        "profile_id": p["profile_id"],
        "profile_name": p["profile_name"],
        "profile": p["profile"],
        "target_kind": "sample",
        "rows": count,
        "storage_format": "lance",
        "storage_kind": "result_table",
        "storage_version": "2.2",
        "schema_version": "v0.1",
        "schema_sha256": digest(schema_description(ds.schema)),
        "table_path": str(
            final.relative_to(
                Path(p["output_root"]) / "datasets" / source["dataset_id"] / source["release_id"]
            )
            / "features.lance"
        ),
        "lance_version": ds.version,
        "input_root": p["root"],
        "inputs": [
            {
                "alias": "samples",
                "artifact_kind": "base",
                "dataset_id": source["dataset_id"],
                "release_id": source["release_id"],
                "table_path": "samples.lance",
                "branch": None,
                "lance_version": source["lance_version"],
                "manifest_path": source["manifest_path"],
                "manifest_sha256": source["manifest_sha256"],
            }
        ],
        "selection": {
            "mode": "all_samples",
            "input_alias": "samples",
            "available_target_rows": source["rows"],
            "target_count": count,
            "target_set_sha256": d["target_set_sha256"],
        },
        "coverage": {
            "total_targets": count,
            "ok": status["ok"],
            "failed": status["failed"],
            "unsupported": 0,
            "skipped": 0,
            "missing": 0,
        },
        "error_counts": dict(sum((Counter(c["error_counts"]) for c in checkpoints), Counter())),
        "validation": {
            "unique_targets": count,
            "payloads_checked": status["ok"],
            "fingerprints_checked": count,
            "readback": "every fragment file read back and compared to the written table",
        },
        "execution": {
            "code_sha256": p["execution_code_sha256"],
            "requested_gpus": p.get("gpus", []),
            "workers_per_gpu": p.get("workers_per_gpu", 1),
            "cpu_threads_per_worker": p.get("cpu_threads", 8),
            "audio_seconds": sum(c["audio_seconds"] for c in checkpoints),
            "latent_frames": sum(c["latent_frames"] for c in checkpoints),
            "hardware": list(
                {
                    digest(c["hardware"]): c["hardware"] for c in checkpoints if c["hardware"]
                }.values()
            ),
        },
        "cleanup": {
            "removed_files": removed,
            "audit_path": "cleanup.jsonl" if (stage / "cleanup.jsonl").exists() else None,
        },
        "finished_at": datetime.now(BEIJING).isoformat(),
    }
    validate_feature_coverage(manifest)
    write_json(stage / "manifest.json", manifest)
    (stage / "execution.json").unlink(missing_ok=True)
    stage.rename(final)
    return manifest


def run(work, gpus, *, workers_per_gpu=1, cpu_threads=8, max_tasks=None):
    """Resume verified checkpoints; `max_tasks` bounds a smoke or recovery launch."""
    if workers_per_gpu < 1 or not gpus or len(set(gpus)) != len(gpus):
        raise ValueError("Invalid GPUs or worker count")
    work = Path(work)
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = json.loads((work / "plan.json").read_text())
        validate_plan(p)
        owner = digest(p)
        p.update(gpus=gpus, workers_per_gpu=workers_per_gpu, cpu_threads=cpu_threads)
        jobs, done, audio_seconds = [], 0, 0.0
        for d in p["datasets"]:
            if Path(d["final"]).exists():
                published_manifest(p, d)
                done += d["target_count"]
                continue
            stage = Path(d["stage"])
            reservation = stage / "execution.json"
            if stage.exists() and not reservation.exists():
                raise ValueError("Existing staging directory has no owner")
            stage.mkdir(parents=True, exist_ok=True)
            if reservation.exists() and json.loads(reservation.read_text())["owner"] != owner:
                raise ValueError("Output belongs to another plan")
            write_json(reservation, dict(owner=owner, plan=str(work / "plan.json")))
            with ThreadPoolExecutor(max_workers=8) as verifier:
                checkpoints = list(verifier.map(lambda t: verify_checkpoint(d, t), d["tasks"]))
            for task, c in zip(d["tasks"], checkpoints, strict=True):
                if c is None:
                    jobs.append((d["source"]["dataset_id"], task))
                elif c["profile_id"] != p["profile_id"]:
                    raise ValueError("Checkpoint profile changed")
                else:
                    done += c["rows"]
                    audio_seconds += c["audio_seconds"]
        total = sum(d["target_count"] for d in p["datasets"])
        initial = done
        random.Random(48000).shuffle(jobs)
        limited = max_tasks is not None and len(jobs) > max_tasks
        if max_tasks is not None:
            jobs = jobs[:max_tasks]
        slots = [gpu for _ in range(workers_per_gpu) for gpu in gpus]
        event(work, "run", rows_done=done, rows_total=total, gpus=gpus, tasks=len(jobs))
        started = time.perf_counter()
        if jobs:
            ctx = multiprocessing.get_context("spawn")
            job_queue, results = ctx.Queue(), ctx.Queue()
            for item in jobs:
                job_queue.put(item)
            for _ in slots:
                job_queue.put(None)
            worker_plan = dict(
                p, datasets=[{k: v for k, v in d.items() if k != "tasks"} for d in p["datasets"]]
            )
            processes = [
                ctx.Process(target=worker, args=(gpu, worker_plan, job_queue, results, cpu_threads))
                for gpu in slots
            ]
            for process in processes:
                process.start()
            try:
                finished = 0
                while finished < len(processes):
                    if any(process.exitcode not in (None, 0) for process in processes):
                        raise RuntimeError("dots_vae worker exited without a checkpoint")
                    try:
                        kind, value = results.get(timeout=30)
                    except queue.Empty:
                        if all(not process.is_alive() for process in processes):
                            raise RuntimeError("Workers exited before acknowledging completion")
                        continue
                    if kind == "error":
                        raise RuntimeError(value["traceback"])
                    if kind == "done":
                        finished += 1
                        continue
                    done += value["rows"]
                    audio_seconds += value["audio_seconds"]
                    event(
                        work,
                        "run",
                        rows_done=done,
                        rows_total=total,
                        audio_hours_done=audio_seconds / 3600,
                        rows_per_second=(done - initial) / (time.perf_counter() - started),
                        last_task_seconds=value["wall_seconds"],
                    )
                for process in processes:
                    process.join()
                    if process.exitcode:
                        raise RuntimeError("dots_vae worker failed at shutdown")
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                for process in processes:
                    process.join()
                job_queue.cancel_join_thread()
                job_queue.close()
                results.close()
        if limited:
            event(work, "checkpointed", rows_done=done, rows_total=total)
            return
        for d in p["datasets"]:
            event(work, "publishing", dataset=d["source"]["dataset_id"])
            manifest = finalize(p, d)
            event(work, "dataset_published", dataset=manifest["dataset_id"], rows=manifest["rows"])
        event(work, "complete", rows=total)
