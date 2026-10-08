"""Pinned selection -> bounded seven-GPU stream -> resumable, audited feature tables."""

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

from ...contract import schema_description, speaker_embedding_schema
from ...feature_contract import make_feature_run_id, validate_feature_coverage
from ...feature_runtime import (
    event,
    file_hash,
    open_source,
    ordered_hash,
    published_manifest,
    sorted_set_hash,
    verify_checkpoint,
    write_json,
)
from ...schema import digest
from ...timestamps import BEIJING
from ...writer import remove_uncommitted_data_files
from .packed_encoder import Encoder
from .profile import PROFILE_NAME, profile
from .storage import make_table, validate_table

INPUT_COLUMNS = [
    "sample_id",
    "audio",
    "audio_sha256",
    "sample_rate",
    "channels",
    "num_frames",
    "parent_sample_id",
    "segment_start_frame",
    "segment_end_frame",
]


def prepare(
    targets_plan,
    work,
    model_root,
    acceptance,
    *,
    output_root=None,
    datasets=None,
    reference_sources_plan=None,
    reference_seed=20261008,
):
    """Reuse the already-audited pinned codec target plan, never its codec feature outputs."""
    work = Path(work).resolve()
    original = json.loads(Path(targets_plan).read_text())
    definition = profile(model_root, reference_seed if reference_sources_plan else None)
    reference_sources = {}
    if reference_sources_plan:
        reference_sources = {
            d["source"]["dataset_id"]: d["sources"]["codec"]
            for d in json.loads(Path(reference_sources_plan).read_text())["datasets"]
        }
    profile_name = PROFILE_NAME
    if reference_sources_plan:
        from .reference import PROFILE_NAME as profile_name
    accepted = json.loads(Path(acceptance).read_text())
    if not accepted.get("production_speaker_accepted") or accepted["profile_id"] != digest(
        definition
    ):
        raise ValueError("Speaker acceptance must bind this exact production profile")
    for evidence in accepted["evidence"]:
        if file_hash(evidence["path"]) != evidence["sha256"]:
            raise ValueError("Speaker acceptance evidence changed")
    if file_hash(original["selection_path"]) != original["selection_sha256"]:
        raise ValueError("Pinned selection manifest changed")
    selected = json.loads(Path(original["selection_path"]).read_text())
    if selected["status"] != "complete":
        raise ValueError("Selection is not complete")
    created = datetime.now(BEIJING).isoformat()
    output_root = str(Path(output_root or original["root"]).resolve())
    p = dict(
        root=original["root"],
        output_root=output_root,
        work=str(work),
        model_root=str(Path(model_root).resolve()),
        created_at=created,
        selection_path=original["selection_path"],
        selection_sha256=original["selection_sha256"],
        inputs=original["inputs"],
        profile=definition,
        profile_id=digest(definition),
        profile_name=profile_name,
        acceptance_path=str(Path(acceptance).resolve()),
        acceptance_sha256=file_hash(acceptance),
        execution_code_sha256=file_hash(__file__),
        target_plan_sha256=file_hash(targets_plan),
        datasets=[],
    )
    for source_plan in original["datasets"]:
        source = source_plan["source"]
        name = source["dataset_id"]
        if datasets is not None and name not in datasets:
            continue
        if source not in selected["outputs"]:
            raise ValueError("Target plan source disagrees with selection manifest")
        d = {
            key: source_plan[key]
            for key in ("source", "target_count", "target_set_sha256", "tasks")
        }
        if (
            sum(t["rows"] for t in d["tasks"]) != d["target_count"]
            or d["target_count"] != source["selected_rows"]
        ):
            raise ValueError("Target plan row coverage disagrees")
        if reference_sources_plan:
            d["reference_codec_source"] = reference_sources[name]
            offset = 0
            d["tasks"] = [dict(t) for t in d["tasks"]]
            for t in d["tasks"]:
                t["reference_offset"] = offset
                offset += t["rows"]
        run_id = make_feature_run_id(name, "speaker_embedding", profile_name, created)
        release = Path(output_root) / "datasets" / name / source["release_id"]
        final = release / "features/speaker_embedding" / run_id
        d.update(
            run_id=run_id,
            final=str(final),
            stage=str(final.with_name(run_id + ".incomplete")),
            state=str(
                release.parent
                / ".state"
                / source["release_id"]
                / "features/speaker_embedding"
                / run_id
            ),
        )
        p["datasets"].append(d)
    if not p["datasets"] or (
        datasets and set(datasets) != {d["source"]["dataset_id"] for d in p["datasets"]}
    ):
        raise ValueError("Unknown or empty dataset selection")
    work.mkdir(parents=True, exist_ok=False)
    write_json(work / "plan.json", p)
    validate_plan(p)
    event(
        work,
        "planned",
        rows_total=sum(d["target_count"] for d in p["datasets"]),
        tasks=sum(len(d["tasks"]) for d in p["datasets"]),
    )
    return p


def validate_plan(p):
    if (
        file_hash(p["selection_path"]) != p["selection_sha256"]
        or file_hash(p["acceptance_path"]) != p["acceptance_sha256"]
        or file_hash(__file__) != p["execution_code_sha256"]
        or profile(p["model_root"], p["profile"].get("reference", {}).get("seed")) != p["profile"]
    ):
        raise ValueError("Pinned input, implementation, or numerical profile changed")
    accepted = json.loads(Path(p["acceptance_path"]).read_text())
    if not accepted.get("production_speaker_accepted") or accepted["profile_id"] != p["profile_id"]:
        raise ValueError("Speaker acceptance changed")
    for item in accepted["evidence"]:
        if file_hash(item["path"]) != item["sha256"]:
            raise ValueError("Acceptance evidence changed")
    for d in p["datasets"]:
        if "reference_codec_source" in d:
            item = d["reference_codec_source"]
            if file_hash(item["manifest_path"]) != item["manifest_sha256"]:
                raise ValueError("Pinned reference codec changed")
    for item in p["inputs"]:
        if file_hash(Path(p["root"]) / item["manifest_path"]) != item["manifest_sha256"]:
            raise ValueError("Base manifest changed")


def read_task(p, d, task):
    started = time.perf_counter()
    ds = open_source(p["root"], d["source"])
    rows = (
        ds.scanner(
            fragments=[ds.get_fragment(task["fragment"])],
            filter="selection_reason = 0",
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
    if (
        len(rows) != task["rows"]
        or ordered_hash([r["sample_id"] for r in rows]) != task["ordered_ids_sha256"]
    ):
        raise ValueError("Task target set changed")
    if "reference_codec_source" in d:
        from .reference import attach

        attach(
            rows,
            d["reference_codec_source"],
            task,
            d["state"],
            p["profile"]["reference"]["seed"],
            d["source"]["dataset_id"],
            d["source"]["release_id"],
        )
    return rows, started


def write_task(p, d, task, rows, values, started, hardware):
    table = make_table(rows, values, p["profile"])
    destination = Path(d["stage"]) / "features.lance"
    fragments = write_fragments(
        table,
        destination,
        schema=table.schema,
        mode="create",
        data_storage_version="2.2",
        max_bytes_per_file=1024**3,
    )
    files = []
    batches = []
    for fragment in fragments:
        for data_file in fragment.files:
            path = destination / "data" / data_file.path
            files.append(dict(name=path.name, bytes=path.stat().st_size, sha256=file_hash(path)))
            batches.extend(LanceFileReader(str(path)).read_all(batch_size=1024).to_batches())
    back = pa.Table.from_batches(batches, schema=table.schema)
    if not back.equals(table):
        raise ValueError("Speaker fragment readback differs")
    validate_table(back)
    checkpoint = dict(
        task=task,
        profile_id=p["profile_id"],
        fragments=[f.to_json() for f in fragments],
        files=files,
        rows=len(rows),
        audio_seconds=sum(r["num_frames"] / r["sample_rate"] for r in rows),
        wall_seconds=time.perf_counter() - started,
        payloads_validated=True,
        hardware=hardware,
        error_counts=dict(Counter(error for _, _, error in values if error)),
    )
    if "reference" in p["profile"]:
        reference_path = Path(d["state"]) / "references" / f"{task['id']}.arrow"
        checkpoint["reference_plan_sha256"] = file_hash(reference_path)
        checkpoint["reference_audio_seconds"] = sum(
            (info["end_frame"] - info["start_frame"]) / r["sample_rate"]
            for r, (info, _, error) in zip(rows, values, strict=True)
            if not error
        )
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", checkpoint)
    return checkpoint


def worker(gpu, p, jobs, results, decode_threads):
    """One GPU owner; one read ahead and one validated write behind."""
    try:
        pa.set_cpu_count(2)
        pa.set_io_thread_count(2)
        encoder = Encoder(
            p["model_root"],
            gpu,
            memory_fraction=p.get("memory_fraction", 0.25),
            mel_frame_budget=p.get("mel_frame_budget", 90000),
        )
        sources = {d["source"]["dataset_id"]: d for d in p["datasets"]}
        with (
            ThreadPoolExecutor(max_workers=decode_threads) as decoders,
            ThreadPoolExecutor(max_workers=1) as reader,
            ThreadPoolExecutor(max_workers=1) as writer,
        ):
            item = jobs.get()
            if item is None:
                results.put(("done", gpu))
                return
            future = reader.submit(read_task, p, sources[item[0]], item[1])
            written = None
            while item is not None:
                rows, started = future.result()
                following = jobs.get()
                if following is not None:
                    future = reader.submit(read_task, p, sources[following[0]], following[1])
                values = encoder.extract(rows, decoders)
                if "reference" in p["profile"]:
                    unexpected = sum(
                        bool(error) and error != "no_legal_reference" for _, _, error in values
                    )
                    if unexpected >= max(16, len(values) // 4):
                        raise RuntimeError(
                            "Widespread reference extraction failures; task not committed"
                        )
                if written is not None:
                    results.put(("checkpoint", written.result()))
                written = writer.submit(
                    write_task,
                    p,
                    sources[item[0]],
                    item[1],
                    rows,
                    values,
                    started,
                    encoder.hardware,
                )
                item = following
            if written is not None:
                results.put(("checkpoint", written.result()))
        results.put(("done", gpu))
    except BaseException:
        results.put(("error", dict(gpu=gpu, traceback=traceback.format_exc())))
        raise


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
    dimension = p["profile"]["output"]["embedding_dim"]
    schema = speaker_embedding_schema(dimension)
    if "reference" in p["profile"]:
        from .reference import schema as reference_schema

        schema = reference_schema(dimension)
    ds = lance.LanceDataset.commit(
        destination,
        lance.LanceOperation.Overwrite(schema, fragments),
        read_version=previous,
    )
    ids = np.empty(d["target_count"], dtype="S64")
    keys = np.empty(d["target_count"], dtype="S64")
    count = 0
    status = Counter()
    # Every payload/fingerprint was validated before checkpointing. Rehashing its
    # immutable file above binds that audit; publication need only scan identities.
    for batch in ds.to_batches(
        columns=["target_id", "feature_key", "profile_id", "status"], batch_size=16384
    ):
        for row in batch.to_pylist():
            if row["profile_id"] != p["profile_id"]:
                raise ValueError("Mixed profiles")
            if count >= len(ids):
                raise ValueError("Unexpected extra targets")
            ids[count] = row["target_id"]
            keys[count] = row["feature_key"]
            status[row["status"]] += 1
            count += 1
    if count != d["target_count"] or sorted_set_hash(ids) != d["target_set_sha256"]:
        raise ValueError("Published target set disagrees")
    # The current selection chooses at most one eligible target per exact audio.
    # Stop on violations rather than silently creating multiple authoritative results.
    keys.sort()
    if np.any(keys[1:] == keys[:-1]):
        raise ValueError("Duplicate feature key in selection")
    ds.create_scalar_index("target_id", "BTREE")
    ds.create_scalar_index("feature_key", "BTREE")
    # No external consumer may reference this incomplete directory. A crash after
    # tagging but before rename can therefore replace only this unpublished tag.
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
        "kind": "speaker_embedding",
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
                **{
                    k: source[k]
                    for k in ("dataset_id", "release_id", "table_path", "branch", "lance_version")
                },
                **{
                    k: next(i for i in p["inputs"] if i["dataset_id"] == source["dataset_id"])[k]
                    for k in ("manifest_path", "manifest_sha256")
                },
                "alias": "samples",
                "artifact_kind": "base",
            }
        ],
        "selection": {
            "mode": "selection_branch",
            "input_alias": "samples",
            "filter": "selection_reason = 0",
            "available_target_rows": source["rows"],
            "target_count": count,
            "target_set_sha256": d["target_set_sha256"],
            "manifest_path": str(Path(p["selection_path"]).relative_to(p["root"])),
            "manifest_sha256": p["selection_sha256"],
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
            "same_key_payload_conflicts": 0,
            "acceptance_sha256": p["acceptance_sha256"],
        },
        "execution": {
            "code_sha256": p["execution_code_sha256"],
            "code_migrations": p.get("code_migrations", []),
            "batch_semantics": "concatenate_actual_frames_with_per_sample_offsets",
            "max_encoder_batch_size": 64,
            "requested_gpus": p.get("gpus", []),
            "workers_per_gpu": p.get("workers_per_gpu", 1),
            "decode_threads_per_worker": p.get("decode_threads", 16),
            "mel_frame_budget": p.get("mel_frame_budget", 90000),
            "memory_fraction_per_worker": p.get("memory_fraction", 0.25),
            "canonical_result_policy": "one_selected_target_per_audio",
            "audio_seconds": sum(c["audio_seconds"] for c in checkpoints),
            "hardware": list({digest(c["hardware"]): c["hardware"] for c in checkpoints}.values()),
        },
        "cleanup": {
            "removed_files": removed,
            "audit_path": "cleanup.jsonl" if (stage / "cleanup.jsonl").exists() else None,
        },
        "finished_at": datetime.now(BEIJING).isoformat(),
    }
    if "reference_codec_source" in d:
        manifest["inputs"].append(
            dict(alias="reference_codec", artifact_kind="feature", **d["reference_codec_source"])
        )
        manifest["execution"]["reference_audio_seconds"] = sum(
            c["reference_audio_seconds"] for c in checkpoints
        )
    validate_feature_coverage(manifest)
    write_json(stage / "manifest.json", manifest)
    stage.rename(final)
    return manifest


def run(
    work,
    gpus,
    decode_threads=16,
    *,
    max_tasks=None,
    memory_fraction=0.25,
    mel_frame_budget=90000,
    workers_per_gpu=1,
):
    """Resume verified checkpoints; max_tasks supports a bounded launch/recovery test."""
    if workers_per_gpu < 1 or len(set(gpus)) != len(gpus):
        raise ValueError("Invalid worker count or duplicate GPU IDs")
    slots = [gpu for _ in range(workers_per_gpu) for gpu in gpus]
    work = Path(work)
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = json.loads((work / "plan.json").read_text())
        validate_plan(p)
        owner = digest(p)
        p["gpus"] = gpus
        p["workers_per_gpu"] = workers_per_gpu
        p["decode_threads"] = decode_threads
        p["memory_fraction"] = memory_fraction
        p["mel_frame_budget"] = mel_frame_budget
        jobs = []
        done = 0
        audio_seconds = 0.0
        for d in p["datasets"]:
            if Path(d["final"]).exists():
                manifest = published_manifest(p, d)
                done += d["target_count"]
                audio_seconds += manifest["execution"]["audio_seconds"]
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
                else:
                    if c["profile_id"] != p["profile_id"]:
                        raise ValueError("Checkpoint profile changed")
                    done += c["rows"]
                    audio_seconds += c["audio_seconds"]
        total = sum(d["target_count"] for d in p["datasets"])
        initial_done = done
        random.Random(93015).shuffle(jobs)
        limited = max_tasks is not None and len(jobs) > max_tasks
        if max_tasks is not None:
            jobs = jobs[:max_tasks]
        event(
            work,
            "encoding",
            rows_done=done,
            rows_total=total,
            gpus=gpus,
            decode_threads=decode_threads,
            workers_per_gpu=workers_per_gpu,
        )
        started = time.perf_counter()
        if jobs:
            ctx = multiprocessing.get_context("spawn")
            job_queue = ctx.Queue()
            results = ctx.Queue()
            for item in jobs:
                job_queue.put(item)
            for _ in slots:
                job_queue.put(None)
            # Tasks travel through the queue; do not pickle the full task catalog
            # into every GPU process during startup.
            worker_plan = dict(
                p, datasets=[{k: v for k, v in d.items() if k != "tasks"} for d in p["datasets"]]
            )
            processes = [
                ctx.Process(
                    target=worker, args=(gpu, worker_plan, job_queue, results, decode_threads)
                )
                for gpu in slots
            ]
            for process in processes:
                process.start()
            try:
                finished = 0
                while finished < len(processes):
                    try:
                        kind, value = results.get(timeout=30)
                    except queue.Empty:
                        if any(process.exitcode not in (None, 0) for process in processes):
                            raise RuntimeError(
                                "Speaker worker exited without a completed checkpoint"
                            )
                        if all(not process.is_alive() for process in processes):
                            raise RuntimeError(
                                "Speaker workers exited before completion acknowledgment"
                            )
                        continue
                    if kind == "error":
                        raise RuntimeError(value["traceback"])
                    if kind == "done":
                        finished += 1
                        continue
                    done += value["rows"]
                    audio_seconds += value["audio_seconds"]
                    elapsed = time.perf_counter() - started
                    event(
                        work,
                        "encoding",
                        rows_done=done,
                        rows_total=total,
                        audio_hours_done=audio_seconds / 3600,
                        rows_per_second=(done - initial_done) / elapsed,
                        last_task_seconds=value["wall_seconds"],
                    )
                for process in processes:
                    process.join()
                    if process.exitcode:
                        raise RuntimeError("Speaker worker failed at shutdown")
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
            event(
                work,
                "publishing",
                dataset=d["source"]["dataset_id"],
                rows_done=done,
                rows_total=total,
            )
            manifest = finalize(p, d)
            event(work, "dataset_published", dataset=manifest["dataset_id"], rows=manifest["rows"])
        event(work, "complete", rows=total)
