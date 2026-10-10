"""Pinned inputs, GPU workers with CPU prefetch, resumable independent checkpoints."""

import fcntl
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import subprocess
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from ...feature_runtime import event, file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from .model import DEFINITION, Encoder, Frontend, infer_records
from .publication import load_checkpoint, publish
from .schema import annotation_schema

DATASETS = ("genshin_voice", "starrail_voice", "wutheringwaves", "zenless_voice")
COLUMNS = ["sample_id", "audio", "audio_sha256", "language", "num_frames"]


def implementation():
    return {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


def prepare(root, work, model_dir, code, output_root=None, sample_rows=0):
    root, work, code, model_dir = map(lambda p: Path(p).resolve(), (root, work, code, model_dir))
    if work.exists():
        raise FileExistsError("Use a new work directory; run resumes an existing plan")
    if sample_rows < 0:
        raise ValueError("Negative sample size")
    profile = dict(
        DEFINITION,
        implementation=implementation(),
        model_files={
            p: file_hash(model_dir / p) for p in ("model.pth.tar", "cmvn.ark", "dict.txt")
        },
        upstream_commit=subprocess.check_output(
            ["git", "-C", str(code), "rev-parse", "HEAD"], text=True
        ).strip(),
        upstream_files={
            str(p.relative_to(code)): file_hash(p)
            for p in sorted((code / "fireredasr2s/fireredlid").rglob("*.py"))
        },
        dependencies={
            p: importlib.metadata.version(p)
            for p in (
                "torch",
                "numpy",
                "scipy",
                "soundfile",
                "pyarrow",
                "pylance",
                "kaldiio",
                "kaldi-native-fbank",
            )
        },
        libsndfile=sf.__libsndfile_version__,
    )
    inputs, groups = [], []
    for name in DATASETS:
        mp = root / "datasets" / name / "v0.1/manifest.json"
        manifest = json.loads(mp.read_text())
        if manifest.get("status") != "complete" or manifest.get("artifact_kind") != "base":
            raise ValueError(f"Input not published: {mp}")
        if manifest.get("validation", {}).get("unique_ids") != manifest["rows"]:
            raise ValueError("Base identity validation missing")
        ds = lance.dataset(mp.parent / manifest["table_path"], version=manifest["lance_version"])
        if ds.count_rows() != manifest["rows"]:
            raise ValueError("Base snapshot row count changed")
        source = dict(
            alias=f"{name}/v0.1",
            role="base",
            dataset_id=name,
            release_id="v0.1",
            manifest_path=str(mp.relative_to(root)),
            manifest_sha256=file_hash(mp),
            table_path=str((mp.parent / manifest["table_path"]).relative_to(root)),
            branch=None,
            lance_version=ds.version,
            base_rows=manifest["rows"],
        )
        fragments = ds.get_fragments()
        if sample_rows:
            fragments = [fragments[i] for i in sorted({0, len(fragments) // 2, len(fragments) - 1})]
        tasks = []
        for fragment in fragments:
            n = fragment.count_rows()
            offsets = [max(0, (n - sample_rows) // 2)] if sample_rows else range(0, n, 1024)
            for offset in offsets:
                task = dict(
                    source=len(inputs),
                    fragment=fragment.fragment_id,
                    offset=offset,
                    rows=min(sample_rows or 1024, n - offset),
                )
                task["id"] = digest(task)
                tasks.append(task)
        source["target_rows"] = sum(t["rows"] for t in tasks)
        inputs.append(source)
        groups.append(tasks)
    tasks = [g[i] for i in range(max(map(len, groups))) for g in groups if i < len(g)]
    now = datetime.now(BEIJING)
    plan = dict(
        root=str(root),
        output_root=str(Path(output_root or root).resolve()),
        run_id="tts-ann-spoken_language-" + now.strftime("%Y%m%dT%H%M%Sbjt") + "-01",
        created_at=now.isoformat(),
        model_dir=str(model_dir),
        upstream_code=str(code),
        inputs=inputs,
        tasks=tasks,
        profile=profile,
        profile_id=digest(profile),
        scope="fixed_fragment_subsets" if sample_rows else "all_base_samples_four_games",
    )
    work.mkdir(parents=True)
    write_json(work / "plan.json", plan)
    return plan


@lru_cache(maxsize=4)
def source_table(path, version):
    return lance.dataset(path, version=version)


def scan(plan, task, columns):
    source = plan["inputs"][task["source"]]
    ds = source_table(str(Path(plan["root"]) / source["table_path"]), source["lance_version"])
    return ds.scanner(
        fragments=[ds.get_fragment(task["fragment"])],
        columns=columns,
        offset=task["offset"],
        limit=task["rows"],
        batch_size=32,
        scan_in_order=True,
        batch_readahead=2,
        fragment_readahead=1,
    ).to_batches()


_STATE = None


def init_worker(plan, work, plan_hash, gpu_queue, batch_size, frame_budget, cpu_threads):
    global _STATE
    gpu = gpu_queue.get()
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu)
    pa.set_cpu_count(1)
    pa.set_io_thread_count(2)
    frontend = Frontend(plan["model_dir"], plan["upstream_code"])
    encoder = Encoder(plan["model_dir"], plan["upstream_code"])
    pool = ThreadPoolExecutor(cpu_threads)
    _STATE = (plan, Path(work), plan_hash, gpu, frontend, encoder, pool, batch_size, frame_budget)


def execute_task(task):
    plan, work, plan_hash, gpu, frontend, encoder, pool, batch_size, frame_budget = _STATE
    start = time.monotonic()
    # Concurrent decode/resample/fbank within a bounded fragment, then duration buckets.
    rows = (r for b in scan(plan, task, COLUMNS) for r in b.to_pylist())
    prepared = list(pool.map(lambda r: frontend.prepare(r, plan["profile_id"]), rows))
    frontend_seconds = time.monotonic() - start
    records = infer_records(encoder, prepared, batch_size, frame_budget)
    expected = [v for b in scan(plan, task, ["sample_id"]) for v in b.column(0).to_pylist()]
    if [r["sample_id"] for r in records] != expected or len(records) != task["rows"]:
        raise ValueError("Checkpoint identities differ from pinned base range")
    if len(records) >= 32 and all(r["status"] != "ok" for r in records):
        raise RuntimeError("Entire task failed; investigate systemic input failures")
    folder = work / "checkpoints" / task["id"]
    folder.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=annotation_schema())
    temp = folder / "results.parquet.tmp"
    pq.write_table(table, temp, compression="zstd")
    if not pq.read_table(temp).equals(table):
        raise ValueError("Checkpoint full-value readback failed")
    temp.replace(folder / "results.parquet")
    checkpoint = dict(
        task=task,
        plan_sha256=plan_hash,
        sha256=file_hash(folder / "results.parquet"),
        counts=dict(Counter(r["status"] for r in records)),
        errors=dict(Counter(r["error_code"] for r in records if r["error_code"])),
        ids_sha256=hashlib.sha256("\n".join(expected).encode()).hexdigest(),
        gpu=gpu,
        frontend_seconds=frontend_seconds,
        elapsed_seconds=time.monotonic() - start,
        analyzed_seconds=sum(
            r["result"]["end_sample"] / r["result"]["native_sample_rate"]
            for r in records
            if r["result"]
        ),
        truncated=sum(r["result"]["analysis_truncated"] for r in records if r["result"]),
    )
    write_json(folder / "checkpoint.json", checkpoint)
    return checkpoint


def verify_plan(plan):
    if (
        plan["profile_id"] != digest(plan["profile"])
        or plan["profile"]["implementation"] != implementation()
    ):
        raise ValueError("Pinned LID implementation/profile changed")
    for name, sha in plan["profile"]["model_files"].items():
        if file_hash(Path(plan["model_dir"]) / name) != sha:
            raise ValueError("Pinned model changed")
    for name, sha in plan["profile"]["upstream_files"].items():
        if file_hash(Path(plan["upstream_code"]) / name) != sha:
            raise ValueError("Upstream inference code changed")
    for name, version in plan["profile"]["dependencies"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Dependency changed: {name}")
    for source in plan["inputs"]:
        if file_hash(Path(plan["root"]) / source["manifest_path"]) != source["manifest_sha256"]:
            raise ValueError("Source manifest changed")


def run(work, gpus, batch_size=32, frame_budget=32000, cpu_threads=8):
    work = Path(work)
    if not gpus or min(batch_size, frame_budget, cpu_threads) < 1:
        raise ValueError("GPU list and positive resource limits required")
    with (work / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((work / "plan.json").read_text())
        verify_plan(plan)
        plan_hash = digest(plan)
        manifest = (
            Path(plan["output_root"])
            / "annotations/spoken_language"
            / plan["run_id"]
            / "manifest.json"
        )
        if manifest.exists():
            event(work, "complete", manifest=str(manifest), resumed_complete=True)
            return
        reservation = manifest.parent / "reservation.json"
        identity = dict(plan_sha256=plan_hash, profile_id=plan["profile_id"])
        reservation.parent.mkdir(parents=True, exist_ok=True)
        try:
            with reservation.open("x") as stream:
                json.dump(identity, stream)
        except FileExistsError:
            if json.loads(reservation.read_text()) != identity:
                raise ValueError("Run ID already reserved by another plan")
        execution = dict(
            gpus=gpus,
            batch_size=batch_size,
            frame_budget=frame_budget,
            cpu_threads_per_worker=cpu_threads,
            started_at=datetime.now(BEIJING).isoformat(),
        )
        write_json(work / "execution.json", execution)
        checkpoints, pending = {}, []
        for task in plan["tasks"]:
            checkpoint = load_checkpoint(work, task, plan_hash)
            if checkpoint is None:
                pending.append(task)
            else:
                checkpoints[task["id"]] = checkpoint
        done = sum(sum(c["counts"].values()) for c in checkpoints.values())
        total = sum(t["rows"] for t in plan["tasks"])
        start, resumed = time.monotonic(), done
        event(work, "running", completed_rows=done, total_rows=total, remaining_tasks=len(pending))
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        for gpu in gpus:
            queue.put(gpu)
        try:
            with ProcessPoolExecutor(
                len(gpus),
                mp_context=context,
                initializer=init_worker,
                initargs=(
                    plan,
                    str(work),
                    plan_hash,
                    queue,
                    batch_size,
                    frame_budget,
                    cpu_threads,
                ),
            ) as executor:
                task_iter = iter(pending)
                futures = {}

                def submit():
                    task = next(task_iter, None)
                    if task is not None:
                        futures[executor.submit(execute_task, task)] = task

                for _ in range(2 * len(gpus)):
                    submit()
                while futures:
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        task = futures.pop(future)
                        checkpoint = future.result()
                        checkpoints[task["id"]] = checkpoint
                        done += sum(checkpoint["counts"].values())
                        elapsed = time.monotonic() - start
                        event(
                            work,
                            "running",
                            completed_rows=done,
                            total_rows=total,
                            failed_rows=sum(
                                c["counts"].get("failed", 0) for c in checkpoints.values()
                            ),
                            rows_per_second=(done - resumed) / elapsed,
                            elapsed_seconds=elapsed,
                            completed_tasks=len(checkpoints),
                            total_tasks=len(plan["tasks"]),
                        )
                        submit()
            manifest = publish(plan, work, checkpoints)
            event(work, "complete", completed_rows=done, total_rows=total, manifest=str(manifest))
        except BaseException as exc:
            event(
                work,
                "stopped",
                completed_rows=done,
                total_rows=total,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
