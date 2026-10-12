"""Pinned selection -> bounded seven-GPU stream -> resumable, audited feature tables.

Published rows of the identical profile are copied by target instead of recomputed: a CPU
pool classifies every task and copies the fully reusable ones while GPU workers extract the
rest. Only the coordinator publishes tables; task identity comes from pinned targets.
"""

import fcntl
import json
import multiprocessing
import queue
import random
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
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
IDENTITY_COLUMNS = [c for c in INPUT_COLUMNS if c != "audio"]
# Stored columns that decide whether a published row answers this plan's question.
REUSE_COLUMNS = [
    "target_id",
    "profile_id",
    "status",
    "error_code",
    "audio_sha256",
    "native_sample_rate",
    "start_frame",
    "end_frame",
    "reference_codec_start",
    "reference_codec_end",
    "reference_native_total_frames",
]
REFERENCE_COORDINATES = REUSE_COLUMNS[6:]
MODES = ("all", "reuse", "extract")


def prepare(
    targets_plan,
    work,
    model_root,
    acceptance,
    *,
    output_root=None,
    datasets=None,
    reference_sources_plan=None,
    reference_codec_plan=None,
    reference_seed=20261008,
    reuse=(),
):
    """Reuse the already-audited pinned codec target plan, never its codec feature outputs.

    `reference_codec_plan` pins each dataset's published codec from a codec run plan;
    `reference_sources_plan` keeps accepting an existing merge plan. `reuse` lists published
    speaker manifests of this exact profile whose rows may be copied by target.
    """
    work = Path(work).resolve()
    original = json.loads(Path(targets_plan).read_text())
    if reference_sources_plan and reference_codec_plan:
        raise ValueError("Pass either a merge plan or a codec plan as the reference source")
    reference = bool(reference_sources_plan or reference_codec_plan)
    definition = profile(model_root, reference_seed if reference else None)
    reference_sources = {}
    if reference_sources_plan:
        reference_sources = {
            d["source"]["dataset_id"]: (d["sources"]["codec"], None)
            for d in json.loads(Path(reference_sources_plan).read_text())["datasets"]
        }
    elif reference_codec_plan:
        reference_sources = codec_sources(reference_codec_plan)
    profile_name = PROFILE_NAME
    if reference:
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
    reuse = reuse_sources(reuse, digest(definition))
    if reuse and not reference:
        raise ValueError("Reuse is defined for the codec-grid reference profile only")
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
        reuse=reuse,
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
        if reference:
            if name not in reference_sources:
                raise ValueError(f"No reference codec for dataset {name}")
            d["reference_codec_source"], target_set = reference_sources[name]
            if target_set is not None and target_set != d["target_set_sha256"]:
                raise ValueError(f"Reference codec targets differ for dataset {name}")
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
            # Work state stays outside the unified root.
            state=str(work / "state" / name),
            reuse=next((r for r in reuse if r["dataset_id"] == name), None),
        )
        p["datasets"].append(d)
    if not p["datasets"] or (
        datasets and set(datasets) != {d["source"]["dataset_id"] for d in p["datasets"]}
    ):
        raise ValueError("Unknown or empty dataset selection")
    planned = {d["source"]["dataset_id"] for d in p["datasets"]}
    if any(r["dataset_id"] not in planned for r in reuse):
        raise ValueError("Reuse source dataset is not part of this plan")
    work.mkdir(parents=True, exist_ok=False)
    write_json(work / "plan.json", p)
    validate_plan(p)
    event(
        work,
        "planned",
        rows_total=sum(d["target_count"] for d in p["datasets"]),
        tasks=sum(len(d["tasks"]) for d in p["datasets"]),
        reuse_sources=len(reuse),
    )
    return p


def codec_sources(plan_path):
    """Pin each dataset's published codec table from a codec run plan."""
    sources = {}
    for d in json.loads(Path(plan_path).read_text())["datasets"]:
        path = Path(d["final"]) / "manifest.json"
        if not path.exists():
            raise ValueError(f"Reference codec is not published: {path}")
        m = json.loads(path.read_text())
        if m.get("status") != "complete" or m.get("kind") != "codec":
            raise ValueError(f"Reference codec is not a complete codec run: {path}")
        item = dict(
            manifest_path=str(path),
            manifest_sha256=file_hash(path),
            table_path=str(path.parent / "features.lance"),
            lance_version=m["lance_version"],
            run_id=m["run_id"],
            profile_id=m["profile_id"],
        )
        sources[d["source"]["dataset_id"]] = (item, m["selection"]["target_set_sha256"])
    return sources


def reuse_sources(manifests, profile_id):
    """Pin published speaker results of exactly this profile, at most one per dataset."""
    sources = []
    for path in manifests:
        path = Path(path).resolve()
        m = json.loads(path.read_text())
        if m.get("status") != "complete" or m.get("kind") != "speaker_embedding":
            raise ValueError(f"Reuse source is not a complete speaker run: {path}")
        if m["profile_id"] != profile_id:
            raise ValueError(f"Reuse source profile differs from this plan: {path}")
        if any(s["dataset_id"] == m["dataset_id"] for s in sources):
            raise ValueError("At most one reuse source per dataset")
        table = path.parent / "features.lance"
        if not table.exists():
            raise ValueError(f"Reuse source table is missing: {table}")
        sources.append(
            dict(
                dataset_id=m["dataset_id"],
                manifest_path=str(path),
                manifest_sha256=file_hash(path),
                table_path=str(table),
                lance_version=m["lance_version"],
                run_id=m["run_id"],
                profile_id=m["profile_id"],
            )
        )
    return sources


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
        if d.get("reuse"):
            if file_hash(d["reuse"]["manifest_path"]) != d["reuse"]["manifest_sha256"]:
                raise ValueError("Reused speaker manifest changed")
    for item in p["inputs"]:
        if file_hash(Path(p["root"]) / item["manifest_path"]) != item["manifest_sha256"]:
            raise ValueError("Base manifest changed")


def table_schema(definition):
    dimension = definition["output"]["embedding_dim"]
    if "reference" in definition:
        from .reference import schema as reference_schema

        return reference_schema(dimension)
    return speaker_embedding_schema(dimension)


def reusable(old, row):
    """A published row is copied only when it answers exactly this plan's question.

    Identity does not involve the codec feature key, so the same audio, native rate and
    reference coordinates give the same input fingerprint. A stored success is copied for
    a legal reference; a stored `no_legal_reference` is copied when the plan agrees. Any
    other stored failure is recomputed.
    """
    if (
        old is None
        or old["audio_sha256"] != row["audio_sha256"]
        or old["native_sample_rate"] != row["sample_rate"]
    ):
        return False
    ref = row["reference"]
    if any(old[k] != ref[k] for k in REFERENCE_COORDINATES):
        return False
    if ref["error_code"] is None:
        return old["status"] == "ok"
    return (
        ref["error_code"] == "no_legal_reference"
        and old["status"] == "failed"
        and old["error_code"] == "no_legal_reference"
    )


def scan_task(ds, task, columns, filter="selection_reason = 0"):
    return (
        ds.scanner(
            fragments=[ds.get_fragment(task["fragment"])],
            filter=filter,
            offset=task["offset"],
            limit=task["rows"],
            columns=columns,
            scan_in_order=True,
            batch_readahead=1,
            fragment_readahead=1,
        )
        .to_table()
        .to_pylist()
    )


def prepare_task(p, d, task):
    """Identity, reference plan and reuse decisions without reading any audio."""
    started = time.perf_counter()
    ds = open_source(p["root"], d["source"])
    reuse = d.get("reuse")
    rows = scan_task(ds, task, IDENTITY_COLUMNS if reuse else INPUT_COLUMNS)
    ids = [r["sample_id"] for r in rows]
    if len(rows) != task["rows"] or ordered_hash(ids) != task["ordered_ids_sha256"]:
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
    copied, todo = reuse_rows(p, reuse, rows) if reuse else (None, list(range(len(rows))))
    return dict(rows=rows, ids=ids, copied=copied, todo=todo, started=started)


def reuse_rows(p, reuse, rows):
    """Arrow-native copy of the published rows this plan may reuse; return (table, todo).

    Payloads move without decoding: the source publication validated them, and the written
    task is revalidated and compared by exact table equality. Only the codec key is rebound.
    """
    old = lance.dataset(reuse["table_path"], version=reuse["lance_version"])
    quoted = ",".join(f"'{r['sample_id']}'" for r in rows)
    table = old.to_table(filter=f"target_id IN ({quoted})")
    stored = {}
    for i, meta in enumerate(table.select(REUSE_COLUMNS).to_pylist()):
        if meta["target_id"] in stored:
            raise ValueError("Reuse source has duplicate targets")
        stored[meta["target_id"]] = (i, meta)
    take, keys, todo = [], [], []
    for i, row in enumerate(rows):
        at, meta = stored.get(row["sample_id"], (None, None))
        if meta is not None and meta["profile_id"] == p["profile_id"] and reusable(meta, row):
            take.append(at)
            keys.append(row["reference"]["reference_codec_feature_key"])
        else:
            todo.append(i)
    if not take:
        return None, todo
    schema = table_schema(p["profile"])
    copied = table.take(pa.array(take, pa.int64()))
    copied = copied.set_column(
        copied.schema.get_field_index("reference_codec_feature_key"),
        "reference_codec_feature_key",
        pa.array(keys, pa.string()),
    )
    copied = copied.select(schema.names).cast(schema)
    return pa.Table.from_arrays(copied.columns, schema=schema), todo


def load_audio(p, d, task, prepared):
    """Read audio only for the rows that need extraction, verifying their identities."""
    rows, todo = prepared["rows"], prepared["todo"]
    if not todo or "audio" in rows[0]:
        return prepared
    ds = open_source(p["root"], d["source"])
    wanted = [rows[i]["sample_id"] for i in todo]
    if len(todo) == len(rows):
        full = scan_task(ds, task, INPUT_COLUMNS)
    else:
        quoted = ",".join(f"'{sid}'" for sid in wanted)
        full = scan_task(
            ds,
            dict(task, offset=0),
            INPUT_COLUMNS,
            filter=f"selection_reason = 0 AND sample_id IN ({quoted})",
        )
    by_id = {r["sample_id"]: r for r in full}
    if len(full) != len(wanted) or set(by_id) != set(wanted):
        raise ValueError("Task audio rows differ from identity rows")
    for i in todo:
        r = by_id[rows[i]["sample_id"]]
        if any(r[k] != rows[i][k] for k in IDENTITY_COLUMNS):
            raise ValueError("Task audio rows differ from identity rows")
        rows[i]["audio"] = r["audio"]
    return prepared


def read_task(p, d, task):
    return load_audio(p, d, task, prepare_task(p, d, task))


def extract(p, prepared, encoder, decoders):
    todo = prepared["todo"]
    if not todo:
        return []
    if encoder is None:
        raise ValueError("Task needs extraction but this worker has no encoder")
    values = encoder.extract([prepared["rows"][i] for i in todo], decoders)
    if "reference" in p["profile"]:
        unexpected = sum(bool(error) and error != "no_legal_reference" for _, _, error in values)
        if unexpected >= max(16, len(values) // 4):
            raise RuntimeError("Widespread reference extraction failures; task not committed")
    return values


def run_task(p, d, task, encoder, decoders):
    """Synchronous form of the worker pipeline: read, extract what is missing, write."""
    prepared = read_task(p, d, task)
    values = extract(p, prepared, encoder, decoders)
    return write_task(p, d, task, prepared, values, getattr(encoder, "hardware", {}))


def reference_seconds(table):
    ok = table.filter(pc.equal(table["status"], "ok"))
    if not ok.num_rows:
        return 0.0
    frames = pc.cast(pc.subtract(ok["end_frame"], ok["start_frame"]), pa.float64())
    return float(pc.sum(pc.divide(frames, pc.cast(ok["native_sample_rate"], pa.float64()))).as_py())


def write_task(p, d, task, prepared, values, hardware):
    rows, ids, todo, copied = (prepared[k] for k in ("rows", "ids", "todo", "copied"))
    parts = [] if copied is None else [copied]
    if todo:
        parts.append(make_table([rows[i] for i in todo], values, p["profile"]))
    table = pa.concat_tables(parts) if len(parts) > 1 else parts[0]
    if len(parts) > 1:
        order = pc.index_in(pa.array(ids, pa.string()), value_set=table["target_id"])
        if order.null_count or table.num_rows != len(ids):
            raise ValueError("Copied and extracted targets differ from the task")
        table = table.take(order)
    if table["target_id"].to_pylist() != ids:
        raise ValueError("Task table order differs from the pinned targets")
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
        reused_rows=len(rows) - len(todo),
        reuse_source=d.get("reuse") and d["reuse"]["manifest_sha256"],
        audio_seconds=sum(r["num_frames"] / r["sample_rate"] for r in rows),
        wall_seconds=time.perf_counter() - prepared["started"],
        payloads_validated=True,
        hardware=dict(hardware) if todo else {},
        error_counts=dict(Counter(e for e in table["error_code"].to_pylist() if e)),
    )
    if "reference" in p["profile"]:
        reference_path = Path(d["state"]) / "references" / f"{task['id']}.arrow"
        checkpoint["reference_plan_sha256"] = file_hash(reference_path)
        checkpoint["reference_audio_seconds"] = reference_seconds(table)
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", checkpoint)
    return checkpoint


def next_job(jobs):
    """Tasks arrive while classification runs; never outlive a killed coordinator."""
    while True:
        try:
            return jobs.get(timeout=30)
        except queue.Empty:
            parent = multiprocessing.parent_process()
            if parent is not None and not parent.is_alive():
                raise RuntimeError("Coordinator exited; releasing the GPU") from None


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
            item = next_job(jobs)
            if item is None:
                results.put(("done", gpu))
                return
            future = reader.submit(read_task, p, sources[item[0]], item[1])
            written = None
            while item is not None:
                prepared = future.result()
                following = next_job(jobs)
                if following is not None:
                    future = reader.submit(read_task, p, sources[following[0]], following[1])
                values = extract(p, prepared, encoder, decoders)
                if written is not None:
                    results.put(("checkpoint", written.result()))
                written = writer.submit(
                    write_task,
                    p,
                    sources[item[0]],
                    item[1],
                    prepared,
                    values,
                    encoder.hardware,
                )
                item = following
            if written is not None:
                results.put(("checkpoint", written.result()))
        results.put(("done", gpu))
    except BaseException:
        results.put(("error", dict(gpu=gpu, traceback=traceback.format_exc())))
        raise


_PLAN = None
_SOURCES = None


def init_cpu_worker(plan_path):
    global _PLAN, _SOURCES
    _PLAN = json.loads(Path(plan_path).read_text())
    _SOURCES = {d["source"]["dataset_id"]: d for d in _PLAN["datasets"]}
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)


def classify(args):
    """CPU side: copy a task whose every target is published; hand the rest to a GPU."""
    dataset, task, copy = args
    p, d = _PLAN, _SOURCES[dataset]
    existing = verify_checkpoint(d, task)
    if existing:
        return "checkpoint", existing
    prepared = prepare_task(p, d, task)
    if prepared["todo"]:
        return "needs_gpu", (dataset, task)
    if not copy:
        return "reusable", (dataset, task)
    return "checkpoint", write_task(p, d, task, prepared, [], {})


def classify_all(plan_path, jobs, cpu_workers, results):
    """Feed classification results and copies into `results`; end with a `classified` mark."""
    try:
        if jobs:
            workers = min(cpu_workers, len(jobs))
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=init_cpu_worker,
                initargs=(plan_path,),
            ) as pool:
                # Bounded submission avoids copying many tasks into futures at once.
                iterator = iter(jobs)
                pending = set()
                for _ in range(min(len(jobs), 2 * workers)):
                    pending.add(pool.submit(classify, next(iterator)))
                while pending:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in finished:
                        results.put(future.result())
                        job = next(iterator, None)
                        if job is not None:
                            pending.add(pool.submit(classify, job))
        results.put(("classified", None))
    except BaseException:
        results.put(("error", dict(gpu=None, traceback=traceback.format_exc())))


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
    schema = table_schema(p["profile"])
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
            "rule": "same profile_id, audio_sha256, native sample rate and reference "
            "coordinates; stored ok or matching no_legal_reference; payload and identity "
            "copied unchanged, reference_codec_feature_key rebound to the pinned codec",
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
    # The ownership marker only guards the unpublished stage; it is not part of the release.
    (stage / "execution.json").unlink(missing_ok=True)
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
    mode="all",
    cpu_workers=8,
):
    """Resume verified checkpoints; max_tasks supports a bounded launch/recovery test.

    `all` classifies every task on a CPU pool, copying fully reusable tasks there while GPU
    workers consume the rest as they are identified. `reuse` only copies (no GPU) and
    `extract` only extracts; they take separate locks so they can run on different machines.
    """
    if mode not in MODES:
        raise ValueError("Unknown run mode")
    if workers_per_gpu < 1 or cpu_workers < 1 or len(set(gpus)) != len(gpus):
        raise ValueError("Invalid worker count or duplicate GPU IDs")
    if mode != "reuse" and not gpus:
        raise ValueError("Extraction needs at least one GPU")
    slots = [gpu for _ in range(workers_per_gpu) for gpu in gpus] if mode != "reuse" else []
    work = Path(work)
    with ExitStack() as locks:
        for name in MODES if mode == "all" else (mode,):
            lock = locks.enter_context((work / f"run-{name}.lock").open("w"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = json.loads((work / "plan.json").read_text())
        validate_plan(p)
        owner = digest(p)
        p["gpus"] = gpus if mode != "reuse" else []
        p["workers_per_gpu"] = workers_per_gpu
        p["decode_threads"] = decode_threads
        p["memory_fraction"] = memory_fraction
        p["mel_frame_budget"] = mel_frame_budget
        p["cpu_workers"] = cpu_workers
        sources = {d["source"]["dataset_id"]: d for d in p["datasets"]}
        jobs = []
        done = 0
        reused = 0
        audio_seconds = 0.0
        for d in p["datasets"]:
            if Path(d["final"]).exists():
                manifest = published_manifest(p, d)
                done += d["target_count"]
                reused += (manifest.get("reuse") or {}).get("rows", 0)
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
                    reused += c.get("reused_rows", 0)
                    audio_seconds += c["audio_seconds"]
        total = sum(d["target_count"] for d in p["datasets"])
        initial_done = done
        random.Random(93015).shuffle(jobs)
        limited = max_tasks is not None and len(jobs) > max_tasks
        if max_tasks is not None:
            jobs = jobs[:max_tasks]
        # Tasks of a dataset without a reuse source go straight to the GPUs; the others
        # are classified (and, unless extracting only, copied) on the CPU pool first.
        classified = [(ds, t, mode != "extract") for ds, t in jobs if sources[ds].get("reuse")]
        direct = [(ds, t) for ds, t in jobs if not sources[ds].get("reuse")]
        event(
            work,
            mode,
            rows_done=done,
            rows_total=total,
            gpus=p["gpus"],
            decode_threads=decode_threads,
            workers_per_gpu=workers_per_gpu,
            cpu_workers=cpu_workers if classified else 0,
            tasks=len(jobs),
        )
        started = time.perf_counter()
        awaiting = 0
        if jobs:
            ctx = multiprocessing.get_context("spawn")
            job_queue = ctx.Queue()
            results = ctx.Queue()
            processes = []
            if slots:
                for item in direct:
                    job_queue.put(item)
                # Tasks travel through the queue; do not pickle the full task catalog
                # into every GPU process during startup.
                worker_plan = dict(
                    p,
                    datasets=[{k: v for k, v in d.items() if k != "tasks"} for d in p["datasets"]],
                )
                processes = [
                    ctx.Process(
                        target=worker, args=(gpu, worker_plan, job_queue, results, decode_threads)
                    )
                    for gpu in slots
                ]
                for process in processes:
                    process.start()
            else:
                awaiting += len(direct)
            classifier = threading.Thread(
                target=classify_all,
                args=(work / "plan.json", classified, cpu_workers, results),
                daemon=True,
            )
            classifier.start()
            try:
                finished = 0
                sentinels_sent = False
                while finished < len(processes) or not sentinels_sent:
                    # A dead worker stops the run at once, even while CPU copies keep
                    # streaming results; a rerun resumes from verified checkpoints.
                    if any(process.exitcode not in (None, 0) for process in processes):
                        raise RuntimeError("Speaker worker exited without a completed checkpoint")
                    try:
                        kind, value = results.get(timeout=30)
                    except queue.Empty:
                        if finished < len(processes) and all(
                            not process.is_alive() for process in processes
                        ):
                            raise RuntimeError(
                                "Speaker workers exited before completion acknowledgment"
                            )
                        if not sentinels_sent and not classifier.is_alive():
                            raise RuntimeError("Task classification ended without a result")
                        continue
                    if kind == "error":
                        raise RuntimeError(value["traceback"])
                    if kind == "done":
                        finished += 1
                        continue
                    if kind == "classified":
                        # GPU workers only stop once every task has been routed.
                        for _ in slots:
                            job_queue.put(None)
                        sentinels_sent = True
                        continue
                    if kind in ("needs_gpu", "reusable"):
                        if kind == "needs_gpu" and slots:
                            job_queue.put(value)
                        else:
                            awaiting += 1
                        continue
                    done += value["rows"]
                    reused += value.get("reused_rows", 0)
                    audio_seconds += value["audio_seconds"]
                    elapsed = time.perf_counter() - started
                    event(
                        work,
                        mode,
                        rows_done=done,
                        rows_total=total,
                        reused_rows=reused,
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
        waiting = 0
        for d in p["datasets"]:
            if not Path(d["final"]).exists() and any(
                verify_checkpoint(d, t) is None for t in d["tasks"]
            ):
                waiting += 1
                continue
            event(
                work,
                "publishing",
                dataset=d["source"]["dataset_id"],
                rows_done=done,
                rows_total=total,
            )
            manifest = finalize(p, d)
            event(work, "dataset_published", dataset=manifest["dataset_id"], rows=manifest["rows"])
        if waiting:
            event(work, "awaiting_other_mode", datasets_waiting=waiting, tasks_awaiting=awaiting)
        else:
            event(work, "complete", rows=total, reused_rows=reused)
