"""Resumable base-snapshot transcription; one coordinator publishes independent tables."""

import fcntl
import hashlib
import importlib.metadata
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from ...contract import annotation_targets_schema
from ...feature_runtime import event, file_hash, ordered_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from . import audio, client, journal, publication, transport
from .languages import LANGUAGE_CODES
from .languages import SOURCE_LANGUAGES as LANGUAGES
from .schema import result_schema

COLUMNS = ["sample_id", "audio", "audio_sha256", "sample_rate", "channels", "language", "text"]


def code_hashes():
    return {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


def plan_ids(batches, dataset_id, batch_size):
    """Stream ordered identities; base ingestion already established global uniqueness."""
    checksum, tasks, pending, offset = hashlib.sha256(), [], [], 0
    for batch in batches:
        ids = batch.column(0)
        if ids.null_count:
            raise ValueError("Null base sample identity")
        values = ids.to_pylist()
        checksum.update(b"".join(s.encode("ascii") for s in values))
        pending.extend(values)
        count = len(pending) // batch_size * batch_size
        for start in range(0, count, batch_size):
            selected = pending[start : start + batch_size]
            tasks.append(
                dict(
                    dataset_id=dataset_id,
                    offset=offset,
                    rows=len(selected),
                    ids_sha256=ordered_hash(selected),
                )
            )
            offset += len(selected)
        pending = pending[count:]
    if pending:
        tasks.append(
            dict(
                dataset_id=dataset_id,
                offset=offset,
                rows=len(pending),
                ids_sha256=ordered_hash(pending),
            )
        )
        offset += len(pending)
    return tasks, offset, checksum.hexdigest()


def prepare(root, work, model_root, endpoint, datasets, batch_size=512):
    if not datasets or len(set(datasets)) != len(datasets) or not 1 <= batch_size <= 1024:
        raise ValueError("Require unique datasets and a batch size in [1,1024]")
    root, work, model_root = Path(root).resolve(), Path(work).resolve(), Path(model_root).resolve()
    if work.exists():
        raise FileExistsError("Use a new work directory; run resumes an existing plan")
    endpoint = endpoint.rstrip("/")
    served = client.check_service(endpoint, "Qwen3-ASR-1.7B", "0.18.0")
    if Path(served["root"]).resolve() != model_root:
        raise ValueError("Server model root differs from the pinned checkpoint")
    files = {
        p.name: file_hash(p)
        for p in sorted(model_root.iterdir())
        if p.suffix in {".json", ".safetensors", ".txt"}
    }
    if not any(n.endswith(".safetensors") for n in files):
        raise ValueError("Model weights not found")
    definition = dict(
        task="transcription",
        name="qwen3-asr-1.7b-base-v2",
        model_files=files,
        server_version="0.18.0",
        checkpoint_dtype="bfloat16",
        service_dtype="auto",
        decode="soundfile_full_float32",
        channels="float32_mean",
        resample="scipy_resample_poly_default_16k",
        request_audio="float32_WAV",
        padding="none_in_client_one_sample_per_request",
        chunking="complete_audio_request_vllm_0.18.0_internal_chunking",
        text_join="newline_between_chunks",
        primary_language="automatic",
        language_candidate="source_language_only_when_nonempty_auto_language_disagrees",
        language_argument="vllm_0.18.0_Qwen_to_language",
        source_language_routes=LANGUAGES,
        original_text_prompt=False,
        temperature=0,
        seed=0,
        max_completion_tokens=1024,
        dependencies={
            n: importlib.metadata.version(n)
            for n in ("numpy", "scipy", "soundfile", "pyarrow", "pylance")
        },
        libsndfile=sf.__libsndfile_version__,
        implementation=code_hashes(),
    )
    inputs, tasks = [], []
    for name in datasets:
        release = root / "datasets" / name / "v0.1"
        mp = release / "manifest.json"
        manifest = json.loads(mp.read_text())
        if manifest["status"] != "complete":
            raise ValueError("Unpublished source")
        if manifest.get("validation", {}).get("unique_ids") != manifest["rows"]:
            raise ValueError("Published base lacks identity uniqueness validation")
        ds = lance.dataset(release / manifest["table_path"], version=manifest["lance_version"])
        source_tasks, rows, ids_sha256 = plan_ids(
            ds.scanner(
                columns=["sample_id"],
                batch_size=65536,
                scan_in_order=True,
                batch_readahead=2,
                fragment_readahead=1,
            ).to_batches(),
            name,
            batch_size,
        )
        if rows != manifest["rows"]:
            raise ValueError("Base target coverage failure")
        source = dict(
            alias=name,
            role="base",
            dataset_id=name,
            release_id="v0.1",
            branch=None,
            manifest_path=str(mp.relative_to(root)),
            manifest_sha256=file_hash(mp),
            table_path=str((release / manifest["table_path"]).relative_to(root)),
            lance_version=manifest["lance_version"],
            rows=rows,
            ordered_ids_sha256=ids_sha256,
        )
        inputs.append(source)
        tasks.extend(source_tasks)
        print(
            json.dumps(
                dict(phase="planned_dataset", dataset_id=name, rows=rows, tasks=len(source_tasks))
            ),
            flush=True,
        )
    created = datetime.now(BEIJING)
    run_id = "tts-ann-transcription-" + created.strftime("%Y%m%dT%H%M%Sbjt") + "-01"
    plan = dict(
        root=str(root),
        model_root=str(model_root),
        endpoint=endpoint,
        model="Qwen3-ASR-1.7B",
        profile=definition,
        profile_id=digest(definition),
        inputs=inputs,
        tasks=sorted(tasks, key=lambda t: (t["offset"], t["dataset_id"])),
        run_id=run_id,
        created_at=created.isoformat(),
    )
    work.mkdir(parents=True)
    write_json(work / "plan.json", plan)
    return plan


def process(row, plan):
    encoded = row["audio"]["bytes"]
    if hashlib.sha256(encoded).hexdigest() != row["audio_sha256"]:
        raise RuntimeError("Source audio hash conflict")
    fingerprint = digest(
        [
            "transcription-input-v1",
            row["sample_id"],
            row["audio_sha256"],
            "full_actual_decode",
            row["language"],
            plan["profile_id"],
        ]
    )
    target = dict(
        target_kind="sample",
        target_id=row["sample_id"],
        input_fingerprint=fingerprint,
        status="ok",
        error_code=None,
        item_count=1,
    )
    try:
        wave, rate, channels = audio.decode(encoded)
    except (ValueError, sf.LibsndfileError) as exc:
        return journal.failed(plan, target, "audio_decode_failed", details=str(exc))
    if rate != row["sample_rate"] or channels != row["channels"]:
        raise RuntimeError("Source audio format conflict")
    expected_language = LANGUAGES.get(row["language"])
    request, frames = audio.encode_request(wave, rate)
    try:
        reply = client.transcribe(plan["endpoint"], plan["model"], request)
    except transport.RequestRejected as exc:
        # Verified Zenless zero-wave placeholders (< 10 ms). Do not suppress arbitrary 400s.
        if (
            exc.status == 400
            # Resampling rounds frame counts up: 439 / 44100 s becomes 160 / 16000 s.
            and len(wave) * 100 < rate
            and not wave.any()
            and "Failed to apply Qwen3ASRProcessor" in exc.body
        ):
            return journal.failed(
                plan,
                target,
                "asr_audio_preprocessing_rejected",
                details=dict(request_frames=frames, http_status=exc.status, response=exc.body),
            )
        journal.record(
            plan,
            target,
            "asr_request_rejected",
            stage="infrastructure",
            details=dict(http_status=exc.status, response=exc.body),
        )
        raise
    except transport.TransientASRError as exc:
        journal.record(
            plan, target, "asr_request_interrupted", stage="infrastructure", details=str(exc)
        )
        raise
    if reply["error_code"]:
        return journal.failed(plan, target, reply["error_code"], details=reply.get("raw_response"))
    mismatch = bool(
        expected_language and reply["language"] and reply["language"] != expected_language
    )
    candidate = None
    if mismatch:
        candidate = client.transcribe(
            plan["endpoint"], plan["model"], request, LANGUAGE_CODES[expected_language]
        )
    if candidate and candidate.get("error_code"):
        journal.record(
            plan,
            target,
            candidate["error_code"],
            stage="language_candidate",
            details=candidate.get("raw_response"),
        )
    result = dict(
        target_kind="sample",
        target_id=row["sample_id"],
        input_fingerprint=fingerprint,
        item_id=0,
        audio_sha256=row["audio_sha256"],
        source_language=row["language"],
        source_text_sha256=digest(row["text"]),
        native_sample_rate=rate,
        start_frame=0,
        end_frame=len(wave),
        text=reply["text"],
        language=reply["language"],
        language_mismatch=mismatch,
        request_frames=frames,
        request_audio_sha256=hashlib.sha256(request).hexdigest(),
        raw_response_json=json.dumps(reply["raw_response"], ensure_ascii=False),
        language_candidate_text=candidate.get("text") if candidate else None,
        language_candidate_response_json=json.dumps(candidate, ensure_ascii=False)
        if candidate
        else None,
    )
    return target, result


def checkpoint_path(work, task):
    return Path(work) / "checkpoints" / task["dataset_id"] / f"{task['offset']:09d}"


def load_checkpoint(work, task, plan_hash):
    folder = checkpoint_path(work, task)
    path = folder / "checkpoint.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value["task"] != task or value["plan_sha256"] != plan_hash:
        raise ValueError("Checkpoint input changed")
    for name, sha in value["files"].items():
        if file_hash(folder / name) != sha:
            raise ValueError("Checkpoint file changed")
    return value


def save_checkpoint(work, task, plan_hash, outputs):
    folder = checkpoint_path(work, task)
    folder.mkdir(parents=True, exist_ok=True)
    targets = pa.Table.from_pylist([t for t, _ in outputs], schema=annotation_targets_schema())
    results = pa.Table.from_pylist([r for _, r in outputs if r is not None], schema=result_schema())
    if (
        len(targets) != task["rows"]
        or ordered_hash(targets["target_id"].to_pylist()) != task["ids_sha256"]
    ):
        raise ValueError("Worker output target mismatch")
    files = {}
    for name, table in (("targets", targets), ("results", results)):
        path = folder / (name + ".parquet")
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
        counts=dict(Counter(targets["status"].to_pylist())),
        language_conflicts=sum(bool(r and r["language_mismatch"]) for _, r in outputs),
    )
    write_json(folder / "checkpoint.json", checkpoint)
    return checkpoint


def publish(plan, work, checkpoints):
    root = Path(plan["root"])
    outputs = []
    for source in plan["inputs"]:
        name = source["dataset_id"]
        selected = [
            (t, c)
            for t, c in zip(plan["tasks"], checkpoints, strict=True)
            if t["dataset_id"] == name
        ]
        counts = Counter()
        for _, c in selected:
            counts.update(c["counts"])
        if sum(counts.values()) != source["rows"]:
            raise ValueError("Publication coverage mismatch")

        def batches():
            for task, _ in selected:
                folder = checkpoint_path(work, task)
                targets = pq.read_table(folder / "targets.parquet")
                results = pq.read_table(folder / "results.parquet")
                yield from publication.combine(targets, results).to_batches(max_chunksize=4096)

        outputs.append(
            publication.publish_output(root, source, plan["run_id"], work, batches, counts)
        )
    directory = root / "annotations/transcription" / plan["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
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
        inputs=plan["inputs"],
        outputs=outputs,
        finished_at=datetime.now(BEIJING).isoformat(),
        code_sha256=code_hashes(),
        execution=json.loads((Path(work) / "execution.json").read_text()),
    )
    path = directory / "manifest.json"
    if path.exists():
        raise FileExistsError("Published annotation is immutable")
    write_json(path, manifest)
    return path


def validate_implementation(work, plan):
    current = code_hashes()
    if current == plan["profile"]["implementation"]:
        return None
    path = Path(work) / "implementation-migration.json"
    if path.exists():
        migration = json.loads(path.read_text())
        if (
            migration["profile_id"] == plan["profile_id"]
            and migration["from"] == plan["profile"]["implementation"]
            and migration["to"] == current
        ):
            return migration
    raise ValueError(
        "Transcription implementation changed; use the frozen code or audited migration"
    )


def run(work, workers=128, auto_resume=False):
    work = Path(work).resolve()
    # Retain ownership through recovery waits as well as active processing.
    with (work / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recovery_count = 0
        write_json(
            work / "supervisor.json",
            dict(
                auto_resume=auto_resume,
                workers=workers,
                retry_after_seconds=60,
                started_at=datetime.now(BEIJING).isoformat(),
            ),
        )
        while True:
            try:
                return _run(work, workers)
            except transport.TransientASRError as exc:
                if not auto_resume:
                    raise
                recovery_count += 1
                previous = json.loads((work / "status.json").read_text())
                event(
                    work,
                    "recovering",
                    verified_rows=previous.get("verified_rows", 0),
                    counts=previous.get("counts", {}),
                    recovery_count=recovery_count,
                    retry_after_seconds=60,
                    error=str(exc),
                )
                with (work / "recovery-events.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            dict(
                                at=datetime.now(BEIJING).isoformat(),
                                recovery_count=recovery_count,
                                error=str(exc),
                            )
                        )
                        + "\n"
                    )
                time.sleep(60)
            except Exception as exc:
                status_path = work / "status.json"
                previous = json.loads(status_path.read_text()) if status_path.exists() else {}
                event(
                    work,
                    "failed",
                    verified_rows=previous.get("verified_rows", 0),
                    counts=previous.get("counts", {}),
                    auto_resume=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise


def _run(work, workers=128):
    work = Path(work).resolve()
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((work / "plan.json").read_text())
        migration = validate_implementation(work, plan)
        if not 1 <= workers <= 128:
            raise ValueError("workers must be in [1,128]")
        plan_hash = file_hash(work / "plan.json")
        plan["_failure_journal"] = journal.FailureJournal(work / "failures.jsonl")
        datasets = {}
        for source in plan["inputs"]:
            if file_hash(Path(plan["root"]) / source["manifest_path"]) != source["manifest_sha256"]:
                raise ValueError("Base manifest changed")
            datasets[source["dataset_id"]] = lance.dataset(
                Path(plan["root"]) / source["table_path"], version=source["lance_version"]
            )
        for name, sha in plan["profile"]["model_files"].items():
            if file_hash(Path(plan["model_root"]) / name) != sha:
                raise ValueError("Model files changed")
        started = time.monotonic()
        write_json(
            work / "execution.json",
            dict(
                endpoint=plan["endpoint"],
                workers=workers,
                timeout_seconds=600,
                request_attempts=transport.ATTEMPTS,
                retry_delays_seconds=list(transport.RETRY_DELAYS),
                code_sha256=code_hashes(),
                implementation_migration=migration,
                started_at=datetime.now(BEIJING).isoformat(),
            ),
        )
        checkpoints, counts, verified, conflicts = [], Counter(), 0, 0
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for index, task in enumerate(plan["tasks"]):
                    checkpoint = load_checkpoint(work, task, plan_hash)
                    if checkpoint is None:
                        served = client.check_service(
                            plan["endpoint"], plan["model"], plan["profile"]["server_version"]
                        )
                        if Path(served["root"]).resolve() != Path(plan["model_root"]):
                            raise RuntimeError("Serving checkpoint changed")
                        table = (
                            datasets[task["dataset_id"]]
                            .scanner(
                                columns=COLUMNS,
                                offset=task["offset"],
                                limit=task["rows"],
                                scan_in_order=True,
                                batch_readahead=1,
                                fragment_readahead=1,
                            )
                            .to_table()
                        )
                        if (
                            len(table) != task["rows"]
                            or ordered_hash(table["sample_id"].to_pylist()) != task["ids_sha256"]
                        ):
                            raise ValueError("Source target order changed")
                        rows = list(pool.map(lambda r: process(r, plan), table.to_pylist()))
                        checkpoint = save_checkpoint(work, task, plan_hash, rows)
                    if checkpoint["counts"].get("failed"):
                        failures = pq.read_table(
                            checkpoint_path(work, task) / "targets.parquet"
                        ).to_pylist()
                        for target in failures:
                            if target["status"] == "failed":
                                plan["_failure_journal"].record(
                                    target,
                                    target["error_code"],
                                    details=dict(
                                        dataset_id=task["dataset_id"], offset=task["offset"]
                                    ),
                                    from_checkpoint=True,
                                )
                    checkpoints.append(checkpoint)
                    verified += task["rows"]
                    counts.update(checkpoint["counts"])
                    conflicts += checkpoint["language_conflicts"]
                    event(
                        work,
                        "running",
                        verified_rows=verified,
                        total_rows=sum(s["rows"] for s in plan["inputs"]),
                        completed_batches=index + 1,
                        total_batches=len(plan["tasks"]),
                        counts=dict(counts),
                        language_conflicts=conflicts,
                        elapsed_seconds=round(time.monotonic() - started, 2),
                    )
            event(work, "publishing", verified_rows=verified, counts=dict(counts))
            path = publish(plan, work, checkpoints)
            event(work, "complete", verified_rows=verified, manifest=str(path), counts=dict(counts))
        except Exception as exc:
            event(
                work,
                "failed",
                verified_rows=verified,
                counts=dict(counts),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
