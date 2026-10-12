"""Pinned selection -> restartable codec fragments -> one published table per dataset.

GPU workers write uncommitted fragments. Only the coordinator publishes tables;
task identity comes from pinned targets, never from the GPU assigned at runtime.
"""

import fcntl
import json
import multiprocessing
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
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
from ...text.metadata import annotation_revisions
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
    reuse=(),
):
    """`reuse` lists published codec manifests whose results may be copied by target."""
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
    # The profile fixes actual decoded lengths (base frame counts are hints only).
    name = f"qwen3-12hz-24k-k16-{precision}-packed-actual-v2"
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
        "reuse": reuse_sources(root, reuse, digest(definition), accepted),
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
                # Work state stays outside the unified root.
                state=str(work / "state" / dataset),
                reuse=next((r for r in p["reuse"] if r["dataset_id"] == dataset), None),
            )
            if d["reuse"]:
                plan_reuse(p, d)
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
        reused_rows=sum(t.get("reuse_rows", 0) for d in p["datasets"] for t in d["tasks"]),
    )
    return p


REUSE_COLUMNS = ["target_id", "status", "audio_sha256", "native_sample_rate", "end_frame"]


def reuse_sources(root, manifests, profile_id, accepted):
    """Pin published codec results of this profile, or of the accepted predecessor."""
    allowed = {profile_id, accepted.get("predecessor_profile_id")} - {None}
    sources = []
    for path in manifests:
        path = Path(path).resolve()
        m = json.loads(path.read_text())
        if m.get("status") != "complete" or m.get("kind") != "codec":
            raise ValueError(f"Reuse source is not a complete codec run: {path}")
        if m["profile_id"] not in allowed:
            raise ValueError(f"Reuse source profile is not accepted: {path}")
        if any(s["dataset_id"] == m["dataset_id"] for s in sources):
            raise ValueError("At most one reuse source per dataset")
        release = Path(root) / "datasets" / m["dataset_id"] / m["release_id"]
        sources.append(
            dict(
                dataset_id=m["dataset_id"],
                manifest_path=str(path.relative_to(root)),
                manifest_sha256=file_hash(path),
                table_path=str((release / m["table_path"]).relative_to(root)),
                lance_version=m["lance_version"],
                profile_id=m["profile_id"],
                same_profile=m["profile_id"] == profile_id,
            )
        )
    return sources


def reusable(old, row, same_profile):
    """A stored result is copied only for the same audio; a predecessor-profile result
    also requires its base-frame timeline to equal the actual decoded length it encoded."""
    return (
        old is not None
        and old["status"] == "ok"
        and old["audio_sha256"] == row["audio_sha256"]
        and old["native_sample_rate"] == row["sample_rate"]
        and (same_profile or old["end_frame"] == row["num_frames"])
    )


def plan_reuse(p, d):
    """Count reusable targets per task so CPU copying and GPU encoding split cleanly.

    Vectorized form of `reusable`; run_task re-applies the scalar rule and must agree.
    """
    r = d["reuse"]
    old = lance.dataset(Path(p["root"]) / r["table_path"], version=r["lance_version"])
    table = old.to_table(columns=REUSE_COLUMNS)
    ids = table["target_id"].to_numpy(zero_copy_only=False).astype("S64")
    order = np.argsort(ids, kind="stable")
    ids = ids[order]
    if len(ids) > 1 and np.any(ids[1:] == ids[:-1]):
        raise ValueError("Reuse source has duplicate targets")
    status = table["status"].to_numpy(zero_copy_only=False)[order]
    audio = table["audio_sha256"].to_numpy(zero_copy_only=False).astype("S64")[order]
    rate = table["native_sample_rate"].to_numpy(zero_copy_only=False)[order]
    end = table["end_frame"].to_numpy(zero_copy_only=False)[order]
    ds = open_source(p["root"], d["source"])
    current, eligible = None, None
    for task in d["tasks"]:
        if task["fragment"] != current:
            current = task["fragment"]
            rows = ds.scanner(
                fragments=[ds.get_fragment(current)],
                columns=["sample_id", "audio_sha256", "sample_rate", "num_frames"],
                filter="selection_reason = 0",
                scan_in_order=True,
            ).to_table()
            sid = rows["sample_id"].to_numpy(zero_copy_only=False).astype("S64")
            at = np.minimum(np.searchsorted(ids, sid), max(len(ids) - 1, 0))
            if len(ids) == 0:
                eligible = np.zeros(len(sid), dtype=bool)
            else:
                eligible = (
                    (ids[at] == sid)
                    & (status[at] == "ok")
                    & (
                        audio[at]
                        == rows["audio_sha256"].to_numpy(zero_copy_only=False).astype("S64")
                    )
                    & (rate[at] == rows["sample_rate"].to_numpy())
                )
                if not r["same_profile"]:
                    eligible &= end[at] == rows["num_frames"].to_numpy()
        task["reuse_rows"] = int(eligible[task["offset"] : task["offset"] + task["rows"]].sum())


def validate_plan(p):
    if (
        file_hash(p["selection_path"]) != p["selection_sha256"]
        or file_hash(p["acceptance_path"]) != p["acceptance_sha256"]
        or file_hash(__file__) != p["execution_code_sha256"]
        or file_hash(Path(__file__).with_name("text.py")) != p["text_code_sha256"]
        or profile(p["model_root"], p["precision"]) != p["profile"]
    ):
        raise ValueError("Pinned input, implementation, or numerical profile changed")
    for i in [*p["inputs"], *p.get("reuse", [])]:
        if file_hash(Path(p["root"]) / i["manifest_path"]) != i["manifest_sha256"]:
            raise ValueError("Base or reused codec manifest changed")
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


IDENTITY_COLUMNS = [c for c in INPUT_COLUMNS if c != "audio"]


def run_task(args):
    """Copy reusable targets as Arrow and encode only the rest, reading only their audio."""
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
            columns=IDENTITY_COLUMNS,
            scan_in_order=True,
        )
        .to_table()
        .to_pylist()
    )
    ids = [r["sample_id"] for r in rows]
    if len(rows) != task["rows"] or ordered_hash(ids) != task["ordered_ids_sha256"]:
        raise ValueError("Task target set changed")
    copied, positions = copy_reusable(p, d, rows)
    if len(positions) != task.get("reuse_rows", 0):
        raise ValueError("Reusable target count differs from plan")
    reused = set(positions)
    need = [i for i in range(len(rows)) if i not in reused]
    encoded, stats = [], dict(encode_seconds=0.0, audio_seconds=0.0, mismatches=[])
    if need:
        if _ENCODER is None:
            raise ValueError("Task needs encoding but this worker has no encoder")
        audio = read_audio(ds, fragment, task, [ids[i] for i in need], len(need) == len(rows))
        encoded = encode_rows(p, [audio[ids[i]] for i in need], stats)
        for row in encoded:
            validate_result(row)
    parts = [copied] if copied is not None else []
    if encoded:
        parts.append(pa.Table.from_pylist(encoded, schema=SCHEMA))
    # Restore task order: copied rows first, then encoded rows, each in task order.
    order = np.argsort(np.asarray(positions + need), kind="stable")
    table = pa.concat_tables(parts).take(pa.array(order))
    if table["target_id"].to_pylist() != ids:
        raise ValueError("Combined task rows lost their order")
    destination = Path(d["stage"]) / "features.lance"
    fragments = write_fragments(
        table,
        destination,
        schema=SCHEMA,
        mode="create",
        data_storage_version="2.2",
        max_bytes_per_file=1024**3,
    )
    files, back = [], []
    for written in fragments:
        for f in written.files:
            path = destination / "data" / f.path
            files.append(
                {"name": path.name, "bytes": path.stat().st_size, "sha256": file_hash(path)}
            )
            back.append(LanceFileReader(str(path)).read_all().to_table())
    if not pa.concat_tables(back).cast(SCHEMA).equals(table):
        raise ValueError("Fragment readback differs")
    copied_seconds = (
        float(
            pc.sum(
                pc.divide(pc.cast(copied["end_frame"], pa.float64()), copied["native_sample_rate"])
            ).as_py()
            or 0.0
        )
        if copied is not None
        else 0.0
    )
    c = {
        "task": task,
        "profile_id": p["profile_id"],
        "fragments": [f.to_json() for f in fragments],
        "files": files,
        "rows": len(rows),
        "reused_rows": len(positions),
        "reuse_source": d.get("reuse") and d["reuse"]["manifest_sha256"],
        "audio_seconds": copied_seconds + stats["audio_seconds"],
        "frame_count_mismatches": stats["mismatches"],
        "encode_seconds": stats["encode_seconds"],
        "wall_seconds": time.perf_counter() - started,
        # Copied payloads were validated by their source publication; copies are checked
        # by exact table equality. Encoded payloads are validated row by row.
        "payloads_validated": True,
        "hardware": getattr(_ENCODER, "execution_hardware", {}) if need else {},
        "worker_quantizer_audit": dict(getattr(_ENCODER, "audit", {})) if need else {},
    }
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", c)
    return c


def quoted_ids(ids):
    """IDs are 64 hex characters; anything else must never reach a filter string."""
    if any(re.fullmatch(r"[0-9a-f]{64}", i) is None for i in ids):
        raise ValueError("Invalid target ID")
    return ",".join("'" + i + "'" for i in ids)


def copy_reusable(p, d, rows):
    """Return (Arrow table of copied rows with identity rebound, their task positions)."""
    r = d.get("reuse")
    if not r:
        return None, []
    old = lance.dataset(Path(p["root"]) / r["table_path"], version=r["lance_version"])
    quoted = quoted_ids([row["sample_id"] for row in rows])
    table = old.to_table(columns=SCHEMA.names, filter=f"target_id IN ({quoted})")
    meta = table.select(REUSE_COLUMNS + ["start_frame", "target_kind", "parent_sample_id"])
    stored = {m["target_id"]: (k, m) for k, m in enumerate(meta.to_pylist())}
    positions, take, rebound = [], [], []
    for i, row in enumerate(rows):
        k, m = stored.get(row["sample_id"], (None, None))
        if reusable(m, row, r["same_profile"]):
            positions.append(i)
            take.append(k)
            rebound.append(relabel(m, p))
    if not positions:
        return None, []
    table = table.take(pa.array(take, type=pa.int64()))
    for name in ("profile_id", "timeline_profile_id", "input_fingerprint", "feature_key"):
        column = pa.array([x[name] for x in rebound], type=SCHEMA.field(name).type)
        table = table.set_column(table.schema.get_field_index(name), name, column)
    return table.select(SCHEMA.names).cast(SCHEMA), positions


def read_audio(ds, fragment, task, ids, whole):
    """Audio rows by sample_id; a whole-task read keeps the positional scan."""
    if whole:
        scan = dict(offset=task["offset"], limit=task["rows"], filter="selection_reason = 0")
    else:
        scan = dict(filter=f"selection_reason = 0 AND sample_id IN ({quoted_ids(ids)})")
    rows = (
        ds.scanner(fragments=[fragment], columns=INPUT_COLUMNS, scan_in_order=True, **scan)
        .to_table()
        .to_pylist()
    )
    found = {r["sample_id"]: r for r in rows}
    if set(found) != set(ids) or len(rows) != len(ids):
        raise ValueError("Audio rows for the task differ from its targets")
    return found


def encode_rows(p, rows, stats):
    from .schedule import batch_indices

    lengths = [(r["num_frames"] * 24000 + r["sample_rate"] - 1) // r["sample_rate"] for r in rows]
    batches = list(batch_indices(lengths))
    results = [None] * len(rows)

    # At most two canonical batches decoded ahead (~960 audio seconds), independent of task size.
    def submit(indices):
        return [(i, _DECODERS.submit(decode, rows[i])) for i in indices]

    pending = submit(batches[0][2]) if batches else []
    for at, (_, _, indices) in enumerate(batches):
        decoded, failed = [], []
        for i, future in pending:
            try:
                decoded.append((i, future.result()))
            except ValueError as exc:
                # A bad sample is a recorded failure, not a task abort that would repeat on
                # every restart; its code is the message prefix (e.g. native_format_mismatch).
                failed.append((i, str(exc).split(":")[0]))
        pending = submit(batches[at + 1][2]) if at + 1 < len(batches) else []
        for i, code in failed:
            results[i] = feature_row(
                rows[i], p["profile"], error=code, native_frames=rows[i]["num_frames"]
            )
        stats["failed"] = stats.get("failed", 0) + len(failed)
        if stats["failed"] >= max(16, len(rows) // 4):
            raise RuntimeError("Widespread decode failures; task not committed")
        t = time.perf_counter()
        outputs = _ENCODER.encode_many([wave for _, (wave, _) in decoded]) if decoded else []
        stats["encode_seconds"] += time.perf_counter() - t
        for (i, (wave, native_frames)), codes in zip(decoded, outputs, strict=True):
            results[i] = feature_row(
                rows[i], p["profile"], wave, codes, native_frames=native_frames
            )
            stats["audio_seconds"] += native_frames / rows[i]["sample_rate"]
            if native_frames != rows[i]["num_frames"]:
                stats["mismatches"].append(
                    dict(
                        sample_id=rows[i]["sample_id"],
                        base_frames=rows[i]["num_frames"],
                        decoded_frames=native_frames,
                    )
                )
    return results


def relabel(old, p):
    """Bind a copied payload to this run's profile with the same identity rules as encoding."""
    row = dict(old)
    row.update(profile_id=p["profile_id"], timeline_profile_id=p["profile"]["timeline_profile_id"])
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
    row["input_fingerprint"] = digest(identity)
    row["feature_key"] = digest(
        [
            "feature-v1",
            row["audio_sha256"],
            row["timeline_profile_id"],
            row["start_frame"],
            row["end_frame"],
            row["profile_id"],
        ]
    )
    return row


def verify_all(d, threads=32):
    """Verify every task checkpoint (rehashing its files) with bounded parallel IO."""
    with ThreadPoolExecutor(max_workers=threads) as pool:
        return list(pool.map(lambda t: verify_checkpoint(d, t), d["tasks"]))


def finalize(p, d):
    stage, final = Path(d["stage"]), Path(d["final"])
    if final.exists():
        return published_manifest(p, d)
    checkpoints = verify_all(d)
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
    # Every payload/fingerprint was validated before checkpointing. Rehashing its
    # immutable file above binds that audit; publication need only scan identities.
    identity = ds.to_table(columns=["target_id", "feature_key", "profile_id", "status"])
    count = identity.num_rows
    if count != d["target_count"]:
        raise ValueError("Unexpected target count")
    profiles = pc.unique(identity["profile_id"]).to_pylist()
    if profiles != [p["profile_id"]]:
        raise ValueError("Mixed profiles")
    ids = identity["target_id"].to_numpy(zero_copy_only=False).astype("S64")
    keys = identity["feature_key"].to_numpy(zero_copy_only=False).astype("S64")
    status = Counter(
        {x["values"]: x["counts"] for x in pc.value_counts(identity["status"]).to_pylist()}
    )
    del identity
    if count != d["target_count"] or sorted_set_hash(ids) != d["target_set_sha256"]:
        raise ValueError("Published target set disagrees")
    # The current selection chooses at most one eligible target per exact audio.
    # Stop on violations rather than silently creating multiple authoritative results.
    keys.sort()
    if np.any(keys[1:] == keys[:-1]):
        raise ValueError("Duplicate feature key in selection")
    selected = json.loads(Path(p["selection_path"]).read_text())
    text_materialization = materialize_text(
        ds,
        open_source(p["root"], d["source"]),
        d["tasks"],
        selected["text_sources"],
        annotation_revisions(p["root"], selected, d["source"]["dataset_id"]),
        # Explicit, small pool: publishing overlaps encoding on a shared CPU quota.
        workers=p.get("text_workers", 12),
        selection_path=p["selection_path"],
        root=p["root"],
        dataset_id=d["source"]["dataset_id"],
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
            "hardware": list(
                {
                    digest(c["hardware"]): c["hardware"] for c in checkpoints if c["hardware"]
                }.values()
            ),
        },
        "reuse": d.get("reuse")
        and {
            **{k: d["reuse"][k] for k in ("manifest_path", "manifest_sha256", "profile_id")},
            "rows": sum(c.get("reused_rows", 0) for c in checkpoints),
            "rule": "same audio_sha256 and sample rate; predecessor profile also requires "
            "base frame count == stored actual end_frame; payload revalidated, identity rebound",
        },
        "cleanup": {
            "removed_files": removed,
            "audit_path": "cleanup.jsonl" if (stage / "cleanup.jsonl").exists() else None,
        },
        "finished_at": datetime.now(BEIJING).isoformat(),
    }
    validate_feature_coverage(manifest)
    write_json(stage / "manifest.json", manifest)
    # The ownership marker only guards the unpublished stage; it is not part of the release.
    (stage / "execution.json").unlink(missing_ok=True)
    stage.rename(final)
    return manifest


MODES = {
    # Tasks whose every target is copied need no GPU; the rest are encoded on GPUs.
    "reuse": lambda t: t.get("reuse_rows", 0) == t["rows"],
    "encode": lambda t: t.get("reuse_rows", 0) < t["rows"],
    "all": lambda t: True,
}


def init_reuse_worker(plan_path):
    global _ENCODER, _DECODERS, _PLAN, _SOURCES
    _PLAN = json.loads(Path(plan_path).read_text())
    _SOURCES = {d["source"]["dataset_id"]: d for d in _PLAN["datasets"]}
    _ENCODER, _DECODERS = None, None
    pa.set_cpu_count(2)
    pa.set_io_thread_count(4)


def run(work, gpus, decode_threads=8, workers_per_gpu=1, *, mode="all", cpu_workers=32):
    """Copy tasks on a CPU pool and encode tasks on GPU workers at the same time.

    Each dataset is published in a background thread as soon as all its tasks have
    checkpoints, so downstream steps can start while other datasets are still encoding.
    `reuse` and `encode` run one side only (separate locks; they never share a task).
    """
    work = Path(work)
    selected_task = MODES[mode]
    with (work / f"run-{mode}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = json.loads((work / "plan.json").read_text())
        validate_plan(p)
        owner = digest(p)
        p["gpus"] = gpus
        p["workers_per_gpu"] = workers_per_gpu
        copy_jobs, gpu_jobs, remaining = [], [], {}
        done, audio_seconds = 0, 0.0
        for d in p["datasets"]:
            name = d["source"]["dataset_id"]
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
            remaining[name] = 0
            for t, c in zip(d["tasks"], verify_all(d), strict=True):
                if c:
                    done += c["rows"]
                    audio_seconds += c["audio_seconds"]
                    continue
                remaining[name] += 1
                if not selected_task(t):
                    continue
                if mode != "encode" and MODES["reuse"](t):
                    copy_jobs.append((name, t))
                else:
                    gpu_jobs.append((name, t))
        # Datasets needing little encoding go first so they publish early.
        work_rows = Counter()
        for name, t in gpu_jobs:
            work_rows[name] += t["rows"] - t.get("reuse_rows", 0)
        gpu_jobs.sort(key=lambda job: work_rows[job[0]])
        if mode == "reuse":
            copy_jobs += gpu_jobs
            gpu_jobs = []
        total = sum(d["target_count"] for d in p["datasets"])
        event(
            work,
            mode,
            rows_done=done,
            rows_total=total,
            copy_tasks=len(copy_jobs),
            gpu_tasks=len(gpu_jobs),
            gpus=gpus if gpu_jobs else [],
            workers_per_gpu=workers_per_gpu,
            cpu_workers=cpu_workers if copy_jobs else 0,
        )
        sources = {d["source"]["dataset_id"]: d for d in p["datasets"]}
        ctx = multiprocessing.get_context("spawn")
        pools, pending = [], {}
        # Publishing a large dataset is long, memory-heavy Python: keep it out of the
        # coordinator so dispatch never stalls; at most two datasets publish at once.
        publisher = ProcessPoolExecutor(max_workers=2, mp_context=ctx)
        succeeded = False
        try:
            published = []

            publish_errors = []

            def announce(future, name):
                error = future.exception()
                if error is None:
                    event(work, "dataset_published", dataset=name, rows=future.result()["rows"])
                else:
                    event(work, "publish_failed", dataset=name, error=str(error))
                    publish_errors.append(error)

            def publish_if_complete(name):
                if remaining.get(name) == 0:
                    remaining.pop(name)
                    future = publisher.submit(finalize, p, sources[name])
                    future.add_done_callback(lambda f, name=name: announce(f, name))
                    published.append(future)

            for name in list(remaining):
                publish_if_complete(name)
            try:
                for jobs, size, initializer, initargs in (
                    (copy_jobs, cpu_workers, init_reuse_worker, (work / "plan.json",)),
                    (gpu_jobs, len(gpus) * workers_per_gpu, init_worker, None),
                ):
                    if not jobs:
                        continue
                    if initargs is None:
                        queue = ctx.Queue()
                        for gpu in gpus * workers_per_gpu:
                            queue.put(gpu)
                        initargs = (queue, work / "plan.json", decode_threads)
                    pool = ProcessPoolExecutor(
                        max_workers=size, mp_context=ctx, initializer=initializer, initargs=initargs
                    )
                    pools.append(pool)
                    # Bounded submission avoids copying the large plan into many futures.
                    iterator = iter(jobs)
                    for _ in range(min(len(jobs), size * 2)):
                        job = next(iterator)
                        pending[pool.submit(run_task, job)] = (job, pool, iterator)
                while pending:
                    if publish_errors:
                        # Fail fast: a later restart re-publishes from checkpoints.
                        raise publish_errors[0]
                    future = next(as_completed(pending))
                    (name, _), pool, iterator = pending.pop(future)
                    c = future.result()
                    done += c["rows"]
                    audio_seconds += c["audio_seconds"]
                    event(
                        work,
                        mode,
                        rows_done=done,
                        rows_total=total,
                        audio_hours_done=audio_seconds / 3600,
                        last_task_seconds=c["wall_seconds"],
                        reused_rows=c.get("reused_rows", 0),
                    )
                    remaining[name] -= 1
                    publish_if_complete(name)
                    job = next(iterator, None)
                    if job is not None:
                        pending[pool.submit(run_task, job)] = (job, pool, iterator)
            finally:
                for pool in pools:
                    pool.shutdown(wait=True, cancel_futures=True)
            for future in published:
                future.result()
            succeeded = True
        finally:
            if not succeeded:
                # Publication is restart-safe (re-commit from checkpoints, atomic rename), so
                # do not wait out a long in-flight publish before reporting the failure.
                for process in list(getattr(publisher, "_processes", {}).values()):
                    process.terminate()
            publisher.shutdown(wait=succeeded, cancel_futures=not succeeded)
        waiting = len(remaining)
        event(work, "complete" if not waiting else "awaiting_other_mode", datasets_waiting=waiting)
