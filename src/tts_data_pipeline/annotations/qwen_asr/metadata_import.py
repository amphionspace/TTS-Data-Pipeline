"""Import pinned Emilia upstream transcripts into the existing v2 sample table."""

import fcntl
import hashlib
import json
import multiprocessing
import time
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path, PurePosixPath

import lance
import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from ...feature_runtime import event, file_hash, ordered_hash, write_json
from ...schema import digest, sample_id
from ...timestamps import BEIJING
from .publication import publish_output
from .schema import annotation_schema

COLUMNS = [
    "sample_id",
    "source_snapshot",
    "source_key",
    "source_locator_json",
    "audio_sha256",
    "sample_rate",
    "language",
    "text",
]
LANGUAGES = {code.upper(): code for code in ("de", "en", "fr", "ja", "ko", "zh")}
_PLAN = None
_BASE = None
_SOURCES = None
_CACHE = OrderedDict()


def implementation():
    folder = Path(__file__).parent
    return {
        name: file_hash(folder / name)
        for name in ("metadata_import.py", "schema.py", "publication.py")
    }


def check_inputs(plan):
    root = Path(plan["root"])
    if file_hash(root / plan["source"]["manifest_path"]) != plan["source"]["manifest_sha256"]:
        raise ValueError("Base manifest changed")
    metadata = Path(plan["metadata_root"])
    if {str(p.relative_to(metadata)) for p in metadata.glob("*/*.json")} != {
        e["path"] for e in plan["inventory"]
    }:
        raise ValueError("Metadata file inventory changed")
    for entry in plan["inventory"]:
        st = (metadata / entry["path"]).stat()
        if (st.st_size, st.st_mtime_ns) != (entry["bytes"], entry["mtime_ns"]):
            raise ValueError(f"Metadata changed: {entry['path']}")
    if implementation() != plan["implementation"]:
        raise ValueError("Importer implementation changed; do not mix checkpoint implementations")


def prepare(root, metadata_root, audit_path, work, *, pilot=False):
    root, metadata_root, work = (Path(p).resolve() for p in (root, metadata_root, work))
    if work.exists():
        raise FileExistsError("Use a new work directory; run resumes a prepared plan")
    mp = root / "datasets/emilia/v0.1/manifest.json"
    base = json.loads(mp.read_text())
    audit = json.loads(Path(audit_path).read_text())
    if base["status"] != "complete" or base["validation"]["unique_ids"] != base["rows"]:
        raise ValueError("Require published unique base identities")
    if not audit["complete"]:
        raise ValueError("Metadata audit is incomplete")
    inputs = {e["path"]: e for e in base["inputs"]}
    inventory = []
    for row in sorted(audit["per_file_results"], key=lambda e: e["path"]):
        archive = str(PurePosixPath(row["path"]).with_suffix(".tar"))
        src = inputs[archive]
        st = (metadata_root / row["path"]).stat()
        if st.st_size != row["bytes"]:
            raise ValueError("Metadata size differs from audit")
        inventory.append(
            dict(
                path=row["path"],
                source_archive=archive,
                source_snapshot=src["source_snapshot"],
                sha256=row["sha256"],
                rows=row["rows"],
                bytes=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
        )
    if len({e["source_snapshot"] for e in inventory}) != len(inventory):
        raise ValueError("Ambiguous source snapshots")
    if {e["source_archive"] for e in inventory} != set(inputs):
        raise ValueError("Metadata archive coverage differs from base")
    if not pilot and sum(e["rows"] for e in inventory) != base["rows"]:
        raise ValueError("Metadata row count differs from base")
    ds = lance.dataset(mp.parent / base["table_path"], version=base["lance_version"])
    checksum, rows = hashlib.sha256(), 0
    for batch in ds.scanner(
        columns=["sample_id"], scan_in_order=True, batch_size=65536
    ).to_batches():
        ids = batch.column(0)
        if ids.null_count:
            raise ValueError("Null base sample identity")
        checksum.update(b"".join(s.encode("ascii") for s in ids.to_pylist()))
        rows += len(ids)
    if rows != base["rows"]:
        raise ValueError("Base row count mismatch")
    source = dict(
        alias="emilia",
        role="base",
        dataset_id="emilia",
        release_id="v0.1",
        branch=None,
        manifest_path=str(mp.relative_to(root)),
        manifest_sha256=file_hash(mp),
        table_path=str((mp.parent / base["table_path"]).relative_to(root)),
        lance_version=base["lance_version"],
        rows=rows,
        ordered_ids_sha256=checksum.hexdigest(),
    )
    profile = dict(
        task="transcription",
        name="qwen3-asr-1.7b-emilia-metadata-v1",
        model="Qwen3-ASR-1.7B",
        model_attribution="user_provided",
        execution_kind="upstream_metadata_import",
        language_source="upstream_metadata",
        language_role="asr_language",
        language_mapping=LANGUAGES,
        text_transform="none",
        timestamps="ignored",
        unknown_inference_fields="null",
        input_fingerprint="transcription-metadata-input-v1",
        upstream_model_revision=None,
        upstream_inference_parameters=None,
    )
    now = datetime.now(BEIJING)
    plan = dict(
        root=str(root),
        metadata_root=str(metadata_root),
        source=source,
        inventory=inventory,
        profile=profile,
        profile_id=digest(profile),
        implementation=implementation(),
        audit_sha256=file_hash(audit_path),
        pilot=pilot,
        run_id="tts-ann-transcription-" + now.strftime("%Y%m%dT%H%M%Sbjt") + "-01",
        created_at=now.isoformat(),
        tasks=[dict(fragment_id=f.fragment_id, rows=f.count_rows()) for f in ds.get_fragments()],
    )
    if sum(t["rows"] for t in plan["tasks"]) != rows:
        raise ValueError("Fragment coverage mismatch")
    check_inputs(plan)
    work.mkdir(parents=True)
    write_json(work / "plan.json", plan)
    event(work, "prepared", rows=rows, fragments=len(plan["tasks"]), run_id=plan["run_id"])
    return plan


def load_metadata(plan, entry):
    path = Path(plan["metadata_root"]) / entry["path"]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError(f"Metadata hash mismatch: {entry['path']}")
    records = orjson.loads(raw)
    if not isinstance(records, list) or len(records) != entry["rows"]:
        raise ValueError(f"Metadata row count mismatch: {entry['path']}")
    result = {}
    for row in records:
        key = row.get("id")
        if not isinstance(key, str) or not key or key in result:
            raise ValueError(f"Invalid or duplicate metadata identity: {entry['path']}: {key}")
        # Do not retain timestamp arrays in the worker cache or annotation.
        result[key] = {k: row.get(k) for k in ("id", "wav", "text", "language")}
    return result


def convert_row(base, record, entry, profile_id):
    key = base["source_key"]
    locator = json.loads(base["source_locator_json"])
    if (
        record["id"] != key
        or base["source_snapshot"] != entry["source_snapshot"]
        or base["sample_id"] != sample_id("emilia", entry["source_snapshot"], key)
        or locator.get("path") != "Emilia/" + entry["source_archive"]
        or not isinstance(record.get("wav"), str)
        or PurePosixPath(record["wav"]).name != PurePosixPath(locator["member"]).name
        or PurePosixPath(record["wav"]).stem != key
    ):
        raise ValueError(f"Source identity conflict: {entry['path']}: {key}")
    fingerprint = digest(
        [
            "transcription-metadata-input-v1",
            base["sample_id"],
            base["audio_sha256"],
            digest(base["text"]),
            base["language"],
            entry["path"],
            entry["sha256"],
            key,
            profile_id,
        ]
    )
    output = dict(
        sample_id=base["sample_id"],
        input_fingerprint=fingerprint,
        status="ok",
        error_code=None,
        result=None,
    )
    raw_language = record.get("language")
    language = LANGUAGES.get(raw_language) if isinstance(raw_language, str) else None
    error = None
    if not isinstance(record.get("text"), str):
        error = "upstream_text_invalid"
    elif language is None:
        error = "upstream_language_invalid"
    if error:
        output.update(status="failed", error_code=error)
        return output
    source_language = base["language"]
    output["result"] = dict(
        audio_sha256=base["audio_sha256"],
        source_language=source_language,
        source_text_sha256=digest(base["text"]),
        text=record["text"],
        language=language,
        native_sample_rate=base["sample_rate"],
        language_mismatch=(language != source_language.lower() if source_language else None),
    )
    return output


def _initialize(plan):
    global _PLAN, _BASE, _SOURCES
    _PLAN = plan
    _BASE = lance.dataset(
        Path(plan["root"]) / plan["source"]["table_path"], version=plan["source"]["lance_version"]
    )
    _SOURCES = {e["source_snapshot"]: e for e in plan["inventory"]}
    _CACHE.clear()


def _checkpoint(work, task):
    return Path(work) / "checkpoints" / f"{task['fragment_id']:06d}.parquet"


def verified_checkpoint(work, task, plan_hash):
    path = _checkpoint(work, task)
    receipt = path.with_suffix(".json")
    if not receipt.exists():
        return None
    value = json.loads(receipt.read_text())
    if (
        value["plan_sha256"] != plan_hash
        or value["rows"] != task["rows"]
        or value["sha256"] != file_hash(path)
    ):
        raise ValueError(f"Checkpoint integrity failure: {path}")
    return value


def _convert_fragment(task, work, plan_hash):
    started = time.monotonic()
    existing = verified_checkpoint(work, task, plan_hash)
    if existing:
        return existing
    table = _BASE.get_fragment(task["fragment_id"]).to_table(columns=COLUMNS)
    if len(table) != task["rows"]:
        raise ValueError("Fragment row count changed")
    outputs, counts, archives, languages = [], Counter(), Counter(), Counter()
    for base in table.to_pylist():
        entry = _SOURCES[base["source_snapshot"]]
        path = entry["path"]
        if path not in _CACHE:
            _CACHE[path] = load_metadata(_PLAN, entry)
            while len(_CACHE) > 2:
                _CACHE.popitem(last=False)
        _CACHE.move_to_end(path)
        record = _CACHE[path].get(base["source_key"])
        if record is None:
            raise ValueError(f"Missing metadata identity: {path}: {base['source_key']}")
        row = convert_row(base, record, entry, _PLAN["profile_id"])
        outputs.append(row)
        counts[row["status"]] += 1
        archives[path] += 1
        languages[base["language"]] += 1
    result = pa.Table.from_pylist(outputs, schema=annotation_schema())
    path = _checkpoint(work, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(result, temporary, compression="zstd")
    if not pq.read_table(temporary).equals(result):
        raise ValueError("Checkpoint roundtrip mismatch")
    temporary.replace(path)
    receipt = dict(
        fragment_id=task["fragment_id"],
        rows=len(result),
        plan_sha256=plan_hash,
        sha256=file_hash(path),
        ordered_ids_sha256=ordered_hash(table["sample_id"].to_pylist()),
        counts=dict(counts),
        archives=dict(archives),
        languages=dict(languages),
        elapsed_seconds=time.monotonic() - started,
    )
    write_json(path.with_suffix(".json"), receipt)
    return receipt


def run(work, workers=8, *, publish=True):
    work = Path(work).resolve()
    if workers < 1:
        raise ValueError("workers must be positive")
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return _run(work, workers, publish)
        except Exception as exc:
            event(work, "failed", error_type=type(exc).__name__, error=str(exc))
            raise


def _run(work, workers, publish):
    plan = json.loads((work / "plan.json").read_text())
    plan_hash = file_hash(work / "plan.json")
    check_inputs(plan)
    counts, archives, languages = Counter(), Counter(), Counter()
    pending, receipts = [], []
    for task in plan["tasks"]:
        receipt = verified_checkpoint(work, task, plan_hash)
        if receipt:
            receipts.append(receipt)
        else:
            pending.append(task)
    for receipt in receipts:
        counts.update(receipt["counts"])
        archives.update(receipt["archives"])
        languages.update(receipt["languages"])
    resumed_rows = sum(counts.values())
    started = time.monotonic()
    event(
        work,
        "importing",
        completed_rows=resumed_rows,
        total_rows=plan["source"]["rows"],
        workers=workers,
        pending_fragments=len(pending),
    )
    with ProcessPoolExecutor(
        workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize,
        initargs=(plan,),
    ) as pool:
        futures = [pool.submit(_convert_fragment, task, str(work), plan_hash) for task in pending]
        for future in as_completed(futures):
            try:
                receipt = future.result()
            except BaseException:
                for pending_future in futures:
                    pending_future.cancel()
                raise
            receipts.append(receipt)
            counts.update(receipt["counts"])
            archives.update(receipt["archives"])
            languages.update(receipt["languages"])
            elapsed = time.monotonic() - started
            event(
                work,
                "importing",
                completed_rows=sum(counts.values()),
                total_rows=plan["source"]["rows"],
                completed_fragments=len(receipts),
                total_fragments=len(plan["tasks"]),
                counts=dict(counts),
                rows_per_second=(sum(counts.values()) - resumed_rows) / max(elapsed, 0.001),
            )
    if sum(counts.values()) != plan["source"]["rows"]:
        raise ValueError("Checkpoint coverage mismatch")
    if not plan["pilot"] and dict(archives) != {e["path"]: e["rows"] for e in plan["inventory"]}:
        raise ValueError("Upstream per-file coverage mismatch")
    check_inputs(plan)
    write_json(
        work / "validation.json",
        dict(
            rows=sum(counts.values()),
            counts=dict(counts),
            languages=dict(languages),
            archives=dict(archives),
            all_inputs_unchanged=True,
            pilot=plan["pilot"],
        ),
    )
    if not publish:
        event(work, "checkpoints_complete", rows=sum(counts.values()))
        return None

    def batches():
        checksum = hashlib.sha256()
        for task in plan["tasks"]:
            table = pq.read_table(_checkpoint(work, task))
            checksum.update(b"".join(s.encode("ascii") for s in table["sample_id"].to_pylist()))
            yield from table.to_batches(max_chunksize=4096)
        if checksum.hexdigest() != plan["source"]["ordered_ids_sha256"]:
            raise ValueError("Checkpoint identities differ from pinned base order")

    event(work, "publishing", rows=sum(counts.values()))
    output = publish_output(plan["root"], plan["source"], plan["run_id"], work, batches, counts)
    check_inputs(plan)
    manifest = dict(
        artifact_kind="annotation",
        contract_version="v0.1",
        schema_version="qwen-asr-transcription-v2",
        status="complete",
        task="transcription",
        target_kind="sample",
        run_id=plan["run_id"],
        profile=plan["profile"],
        profile_id=plan["profile_id"],
        inputs=[plan["source"]],
        outputs=[output],
        finished_at=datetime.now(BEIJING).isoformat(),
        code_sha256=plan["implementation"],
        execution=dict(
            kind="upstream_metadata_import",
            pilot=plan["pilot"],
            workers=workers,
            timestamps="ignored",
            plan_sha256=plan_hash,
        ),
        upstream_metadata=dict(
            root=plan["metadata_root"],
            inventory=plan["inventory"],
            audit_sha256=plan["audit_sha256"],
        ),
    )
    path = Path(plan["root"]) / "annotations/transcription" / plan["run_id"] / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old["execution"]["plan_sha256"] != plan_hash or old["outputs"] != [output]:
            raise ValueError("Published run differs from this import")
    else:
        write_json(path, manifest)
    event(work, "complete", rows=sum(counts.values()), counts=dict(counts), manifest=str(path))
    return path
