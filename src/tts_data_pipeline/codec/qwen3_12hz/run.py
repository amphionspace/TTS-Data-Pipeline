"""Pinned selection -> restartable codec fragments -> one published table per dataset.

GPU workers write uncommitted fragments. Only the coordinator publishes tables;
task identity comes from pinned targets, never from the GPU assigned at runtime.
"""

import fcntl
import json
import multiprocessing
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
from lance.file import LanceFileReader
from lance.fragment import FragmentMetadata, write_fragments

from ...contract import codec_schema, schema_description
from ...feature_contract import make_feature_run_id, validate_feature_coverage
from ...feature_runtime import (
    event,
    file_hash,
    open_source,
    ordered_hash,
    plan_dataset,
    published_manifest,
    sorted_set_hash,
    verify_checkpoint,
    write_json,
)
from ...schema import digest
from ...timestamps import BEIJING
from ...writer import remove_uncommitted_data_files
from .audio import array_sha256, decode, validate_codes
from .encoder import Encoder
from .profile import feature_row, profile
from .text import materialize_text

SCHEMA = codec_schema(16)
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
    root,
    selection_path,
    work,
    model_root,
    acceptance,
    *,
    precision="fp32",
    task_size=4096,
    output_root=None,
    datasets=None,
):
    root, work = Path(root).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=False)
    selection_path = Path(selection_path).resolve()
    selected = json.loads(selection_path.read_text())
    if selected["status"] != "complete":
        raise ValueError("Selection not published")
    accepted = json.loads(Path(acceptance).read_text())
    if not accepted.get(f"official_{precision}_accepted"):
        raise ValueError("Real cross-GPU acceptance must pass before planning production")
    definition = profile(model_root, precision)
    if output_root is None or Path(output_root).resolve() == root:
        if accepted.get("profile_id") != digest(definition):
            raise ValueError("Production planning requires evidence for this exact profile")
        for item in accepted["evidence"]:
            if file_hash(item["path"]) != item["sha256"]:
                raise ValueError("Acceptance evidence changed")
    name = f"qwen3-12hz-24k-k16-{precision}-packed-v1"
    created = datetime.now(BEIJING).isoformat()
    p = {
        "root": str(root),
        "output_root": str(Path(output_root).resolve() if output_root else root),
        "work": str(work),
        "model_root": str(Path(model_root).resolve()),
        "precision": precision,
        "selection_path": str(selection_path),
        "selection_sha256": file_hash(selection_path),
        "profile": definition,
        "profile_id": digest(definition),
        "profile_name": name,
        "created_at": created,
        "acceptance_path": str(Path(acceptance).resolve()),
        "acceptance_sha256": file_hash(acceptance),
        "execution_code_sha256": file_hash(__file__),
        "text_code_sha256": file_hash(Path(__file__).with_name("text.py")),
        "inputs": selected["inputs"],
        "datasets": [],
    }
    outputs = [o for o in selected["outputs"] if datasets is None or o["dataset_id"] in datasets]
    if datasets and {o["dataset_id"] for o in outputs} != set(datasets):
        raise ValueError("Requested dataset is absent from selection")
    event(work, "planning", datasets=len(outputs))
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [pool.submit(plan_dataset, (root, o, task_size, work)) for o in outputs]
        for future in as_completed(futures):
            d = future.result()
            source = d["source"]
            dataset = source["dataset_id"]
            run = make_feature_run_id(dataset, "codec", name, created)
            release = Path(p["output_root"]) / "datasets" / dataset / source["release_id"]
            final = release / "features/codec" / run
            d.update(
                run_id=run,
                final=str(final),
                stage=str(final.with_name(run + ".incomplete")),
                state=str(
                    release.parent / ".state" / source["release_id"] / "features/codec" / run
                ),
            )
            p["datasets"].append(d)
            event(
                work,
                "targets_fixed",
                dataset=dataset,
                rows=d["target_count"],
                tasks=len(d["tasks"]),
            )
    p["datasets"].sort(key=lambda d: d["source"]["dataset_id"])
    write_json(work / "plan.json", p)
    event(
        work,
        "planned",
        target_count=sum(d["target_count"] for d in p["datasets"]),
        tasks=sum(len(d["tasks"]) for d in p["datasets"]),
    )
    return p


def validate_plan(p):
    if (
        file_hash(p["selection_path"]) != p["selection_sha256"]
        or file_hash(p["acceptance_path"]) != p["acceptance_sha256"]
        or file_hash(__file__) != p["execution_code_sha256"]
        or file_hash(Path(__file__).with_name("text.py")) != p["text_code_sha256"]
        or profile(p["model_root"], p["precision"]) != p["profile"]
    ):
        raise ValueError("Pinned input, implementation, or numerical profile changed")
    for i in p["inputs"]:
        if file_hash(Path(p["root"]) / i["manifest_path"]) != i["manifest_sha256"]:
            raise ValueError("Base manifest changed")
    if p["output_root"] == p["root"]:
        accepted = json.loads(Path(p["acceptance_path"]).read_text())
        if accepted.get("profile_id") != p["profile_id"] or not accepted.get(
            "production_codec_accepted"
        ):
            raise ValueError(
                "Production acceptance is incomplete; planning does not authorize publication"
            )
        for item in accepted["evidence"]:
            if file_hash(item["path"]) != item["sha256"]:
                raise ValueError("Acceptance evidence changed")


_ENCODER = None
_DECODERS = None
_PLAN = None
_SOURCES = None


def init_worker(gpus, plan_path, decode_threads):
    global _ENCODER, _DECODERS, _PLAN, _SOURCES
    _PLAN = json.loads(Path(plan_path).read_text())
    _SOURCES = {d["source"]["dataset_id"]: d for d in _PLAN["datasets"]}
    gpu = gpus.get()
    _ENCODER = Encoder(_PLAN["model_root"], gpu, precision=_PLAN["precision"])
    import torch

    device = torch.cuda.get_device_properties(gpu)
    _ENCODER.execution_hardware = {
        "name": device.name,
        "compute_capability": [device.major, device.minor],
        "total_memory": device.total_memory,
        "cuda_runtime": torch.version.cuda,
        "cudnn_version": torch.backends.cudnn.version(),
    }
    _DECODERS = ThreadPoolExecutor(max_workers=decode_threads)
    pa.set_cpu_count(2)
    pa.set_io_thread_count(4)


def validate_result(row):
    identity = {
        k: row[k]
        for k in (
            "target_kind",
            "target_id",
            "parent_sample_id",
            "audio_sha256",
            "timeline_profile_id",
            "native_sample_rate",
            "start_frame",
            "end_frame",
            "profile_id",
        )
    }
    identity.update(task="audio-feature-v1", kind="codec")
    key = [
        "feature-v1",
        row["audio_sha256"],
        row["timeline_profile_id"],
        row["start_frame"],
        row["end_frame"],
        row["profile_id"],
    ]
    if digest(identity) != row["input_fingerprint"] or digest(key) != row["feature_key"]:
        raise ValueError("Stored identity fingerprint mismatch")
    if row["status"] == "ok":
        codes = validate_codes(
            np.asarray(row["codes"], dtype=np.int16), row["encoder_input_num_frames"]
        )
        if array_sha256(codes, ["time", "codebook"]) != row["codes_sha256"]:
            raise ValueError("Stored payload checksum mismatch")
        if row["num_codec_frames"] != len(codes) or row["error_code"] is not None:
            raise ValueError("Stored payload metadata mismatch")
    elif row["status"] != "failed" or not row["error_code"] or row["codes"] is not None:
        raise ValueError("Invalid error record")


def run_task(args):
    dataset, task = args
    p, d = _PLAN, _SOURCES[dataset]
    existing = verify_checkpoint(d, task)
    if existing:
        return existing
    started = time.perf_counter()
    ds = open_source(p["root"], d["source"])
    fragment = ds.get_fragment(task["fragment"])
    rows = (
        ds.scanner(
            fragments=[fragment],
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
    from .schedule import batch_indices

    lengths = [(r["num_frames"] * 24000 + r["sample_rate"] - 1) // r["sample_rate"] for r in rows]
    batches = list(batch_indices(lengths))

    # At most two canonical batches decoded ahead (~960 audio seconds), independent of task size.
    def submit(indices):
        return [(i, _DECODERS.submit(decode, rows[i])) for i in indices]

    pending = submit(batches[0][2]) if batches else []
    results = [None] * len(rows)
    encode_seconds = 0.0
    audio_seconds = 0.0
    frame_count_mismatches = []
    for at, (_, _, indices) in enumerate(batches):
        decoded = [future.result() for _, future in pending]
        waves = [wave for wave, _ in decoded]
        pending = submit(batches[at + 1][2]) if at + 1 < len(batches) else []
        t = time.perf_counter()
        outputs = _ENCODER.encode_many(waves)
        encode_seconds += time.perf_counter() - t
        for i, (wave, native_frames), codes in zip(indices, decoded, outputs, strict=True):
            results[i] = feature_row(
                rows[i], p["profile"], wave, codes, native_frames=native_frames
            )
            audio_seconds += native_frames / rows[i]["sample_rate"]
            if native_frames != rows[i]["num_frames"]:
                frame_count_mismatches.append(
                    dict(
                        sample_id=rows[i]["sample_id"],
                        base_frames=rows[i]["num_frames"],
                        decoded_frames=native_frames,
                    )
                )
    table = pa.Table.from_pylist(results, schema=SCHEMA)
    destination = Path(d["stage"]) / "features.lance"
    fragments = write_fragments(
        table,
        destination,
        schema=SCHEMA,
        mode="create",
        data_storage_version="2.2",
        max_bytes_per_file=1024**3,
    )
    files = []
    readback = []
    for fragment in fragments:
        for f in fragment.files:
            path = destination / "data" / f.path
            files.append(
                {"name": path.name, "bytes": path.stat().st_size, "sha256": file_hash(path)}
            )
            for batch in LanceFileReader(str(path)).read_all(batch_size=128).to_batches():
                readback.extend(batch.to_pylist())
    if readback != results:
        raise ValueError("Fragment readback differs")
    for row in readback:
        validate_result(row)
    c = {
        "task": task,
        "profile_id": p["profile_id"],
        "fragments": [f.to_json() for f in fragments],
        "files": files,
        "rows": len(rows),
        "audio_seconds": audio_seconds,
        "frame_count_mismatches": frame_count_mismatches,
        "encode_seconds": encode_seconds,
        "wall_seconds": time.perf_counter() - started,
        "payloads_validated": True,
        "hardware": getattr(_ENCODER, "execution_hardware", {}),
        "worker_quantizer_audit": dict(getattr(_ENCODER, "audit", {})),
    }
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", c)
    return c


def finalize(p, d):
    stage, final = Path(d["stage"]), Path(d["final"])
    if final.exists():
        return published_manifest(p, d)
    checkpoints = [verify_checkpoint(d, t) for t in d["tasks"]]
    if any(c is None for c in checkpoints):
        raise ValueError("Incomplete target coverage")
    if any(c["profile_id"] != p["profile_id"] for c in checkpoints):
        raise ValueError("Checkpoint profile changed")
    fragments = [
        FragmentMetadata.from_json(json.dumps(f)) for c in checkpoints for f in c["fragments"]
    ]
    destination = stage / "features.lance"
    previous = lance.dataset(destination).version if (destination / "_versions").exists() else 0
    ds = lance.LanceDataset.commit(
        destination, lance.LanceOperation.Overwrite(SCHEMA, fragments), read_version=previous
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
    selected = json.loads(Path(p["selection_path"]).read_text())
    text_materialization = materialize_text(
        ds, open_source(p["root"], d["source"]), d["tasks"], selected["text_sources"]
    )
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
        "kind": "codec",
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
        "text_materialization": text_materialization,
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
        "error_counts": {},
        "validation": {
            "unique_targets": count,
            "payloads_checked": status["ok"],
            "fingerprints_checked": count,
            "same_key_payload_conflicts": 0,
            "acceptance_sha256": p["acceptance_sha256"],
        },
        "execution": {
            "code_sha256": p["execution_code_sha256"],
            "text_code_sha256": p["text_code_sha256"],
            "code_migrations": p.get("code_migrations", []),
            "batch_semantics": "canonical_shape_from_target_length",
            "max_encoder_batch_size": 64,
            "requested_gpus": p.get("gpus", []),
            "workers_per_gpu": p.get("workers_per_gpu", 1),
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
    validate_feature_coverage(manifest)
    write_json(stage / "manifest.json", manifest)
    stage.rename(final)
    return manifest


def run(work, gpus, decode_threads=8, workers_per_gpu=1):
    work = Path(work)
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = json.loads((work / "plan.json").read_text())
        validate_plan(p)
        p["gpus"] = gpus
        p["workers_per_gpu"] = workers_per_gpu
        owner = digest({k: v for k, v in p.items() if k not in {"gpus", "workers_per_gpu"}})
        jobs = []
        done = 0
        audio_seconds = 0
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
            write_json(reservation, {"owner": owner, "plan": str(work / "plan.json")})
            for t in d["tasks"]:
                c = verify_checkpoint(d, t)
                if c:
                    done += c["rows"]
                    audio_seconds += c["audio_seconds"]
                else:
                    jobs.append((d["source"]["dataset_id"], t))
        total = sum(d["target_count"] for d in p["datasets"])
        event(
            work,
            "encoding",
            rows_done=done,
            rows_total=total,
            gpus=gpus,
            workers_per_gpu=workers_per_gpu,
        )
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        slots = gpus * workers_per_gpu
        for gpu in slots:
            queue.put(gpu)
        with ProcessPoolExecutor(
            max_workers=len(slots),
            mp_context=ctx,
            initializer=init_worker,
            initargs=(queue, work / "plan.json", decode_threads),
        ) as pool:
            # Bounded submission avoids copying the large plan into tens of thousands of futures.
            iterator = iter(jobs)
            pending = {}
            for _ in range(min(len(jobs), len(slots) * 2)):
                job = next(iterator)
                pending[pool.submit(run_task, job)] = job
            while pending:
                future = next(as_completed(pending))
                pending.pop(future)
                c = future.result()
                done += c["rows"]
                audio_seconds += c["audio_seconds"]
                event(
                    work,
                    "encoding",
                    rows_done=done,
                    rows_total=total,
                    audio_hours_done=audio_seconds / 3600,
                    last_task_seconds=c["wall_seconds"],
                )
                job = next(iterator, None)
                if job is not None:
                    pending[pool.submit(run_task, job)] = job
        for d in p["datasets"]:
            m = finalize(p, d)
            event(work, "dataset_published", dataset=m["dataset_id"], rows=m["rows"])
        event(work, "complete", rows=total)
