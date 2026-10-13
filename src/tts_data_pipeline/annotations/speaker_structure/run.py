"""MOSS speaker_structure over pinned base datasets, one dataset at a time (spec 05).

prepare pins the base inputs, the service identity and the profile. Every `run` is a new round:
it requests each sample that is not ok yet (all of them at first), with `concurrency` requests
in flight, and rewrites that fragment's checkpoint. `publish` turns the checkpoints into one
results.lance per dataset and the global manifest; failures of the last round are published
as failed rows, and raw responses stay private in the checkpoints.
"""

import fcntl
import hashlib
import importlib.metadata
import json
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.parquet as pq

from ...feature_runtime import event, file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from .publication import TASK, publish_output
from .schema import checkpoint_schema
from .service import Service

PROFILE = "moss-transcribe-diarize-v1"
DATASETS = ("emilia", "emilia_yodas", "emilia2")
COLUMNS = ["sample_id", "audio_sha256", "audio", "sample_rate", "num_frames"]


def implementation():
    folder = Path(__file__).parent
    names = ("run.py", "schema.py", "publication.py", "service.py", "response.py")
    return {name: file_hash(folder / name) for name in names}


def dependencies():
    return {name: importlib.metadata.version(name) for name in ("pylance", "pyarrow", "httpx")}


def profile(base_url, models_sha256, server):
    return dict(
        task=TASK,
        name=PROFILE,
        execution_kind="external_service_inference",
        model=dict(
            id=server["model_id"],
            revision=server["model_revision"],
            served_name=server["served_name"],
            attribution="user_provided",
        ),
        serving=server["serving"],
        service=dict(
            base_url=base_url,
            models_response_sha256=models_sha256,
            identity_check_seconds=600,
            outage_wait_seconds=1800,
            outage_checks=8,
        ),
        request=dict(
            endpoint="/v1/audio/transcriptions",
            form=dict(model=server["served_name"], response_format="verbose_json", temperature="0"),
            audio="full_original_bytes",
            prompt=None,
            language=None,
            timeout_seconds=600,
            concurrency=256,
            resend_attempts=3,
            datasets_in_sequence=True,
        ),
        validation=dict(
            end_tolerance_seconds=1.0,
            truncation_gap_seconds=3.0,
            truncation_gap_fraction=0.3,
        ),
        mapping=dict(
            start="floor(start * sample_rate)",
            end="ceil(end * sample_rate)",
            clip="[0, num_frames]",
            coordinates="base num_frames / sample_rate (reader-reported); no local decode",
            speaker_labels="model labels kept as returned ([S01] -> S01); "
            "observed in first-appearance order",
            turn_order="(start_sample, end_sample, speaker)",
        ),
        overlap_representation="model_can_return_overlapping_turns",
        nondeterminism="results vary slightly with server batching (pilot: speaker_count equal for "
        "99.8% across concurrency 64/128/256; temperature=0); concurrency pinned",
        input_fingerprint="speaker-structure-input-v1",
    )


def pin_source(root, name):
    manifest_path = root / "datasets" / name / "v0.1/manifest.json"
    base = json.loads(manifest_path.read_text())
    if base["status"] != "complete":
        raise ValueError(f"Base {name} is not published complete")
    ds = lance.dataset(manifest_path.parent / base["table_path"], version=base["lance_version"])
    checksum, rows = hashlib.sha256(), 0
    for batch in ds.scanner(
        columns=["sample_id"], scan_in_order=True, batch_size=65536
    ).to_batches():
        checksum.update(b"".join(s.encode("ascii") for s in batch.column(0).to_pylist()))
        rows += len(batch)
    if rows != base["rows"]:
        raise ValueError(f"Base row count mismatch: {name}")
    source = dict(
        alias=name,
        role="base",
        dataset_id=name,
        release_id="v0.1",
        branch=None,
        manifest_path=str(manifest_path.relative_to(root)),
        manifest_sha256=file_hash(manifest_path),
        table_path=str((manifest_path.parent / base["table_path"]).relative_to(root)),
        lance_version=base["lance_version"],
        rows=rows,
        ordered_ids_sha256=checksum.hexdigest(),
    )
    tasks = [
        dict(dataset_id=name, fragment_id=f.fragment_id, rows=f.count_rows())
        for f in ds.get_fragments()
    ]
    return source, tasks


def prepare(root, work, base_url, server_config, datasets=DATASETS):
    root, work = Path(root).resolve(), Path(work).resolve()
    if work.exists():
        raise FileExistsError("Use a new work directory; run resumes a prepared plan")
    server = json.loads(Path(server_config).read_text())
    import httpx

    for attempt in range(5):  # A transient reset must not abort planning.
        try:
            models = httpx.get(base_url + "/v1/models", timeout=60)
            models.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 4:
                raise
            time.sleep(10)
    served = [m["id"] for m in models.json()["data"]]
    if served != [server["served_name"]]:
        raise ValueError(f"Service serves {served}, expected {server['served_name']}")
    sources, tasks = [], []
    for name in datasets:
        source, dataset_tasks = pin_source(root, name)
        sources.append(source)
        tasks += dataset_tasks
    prof = profile(base_url, hashlib.sha256(models.content).hexdigest(), server)
    now = datetime.now(BEIJING)
    plan = dict(
        root=str(root),
        sources=sources,
        profile=prof,
        profile_id=digest(prof),
        implementation=implementation(),
        dependencies=dependencies(),
        server_config_sha256=file_hash(server_config),
        run_id=f"tts-ann-{TASK}-" + now.strftime("%Y%m%dT%H%M%Sbjt") + "-01",
        created_at=now.isoformat(),
        tasks=tasks,
    )
    work.mkdir(parents=True)
    write_json(work / "plan.json", plan)
    event(work, "prepared", rows=sum(s["rows"] for s in sources), fragments=len(tasks))
    return plan


def check_inputs(plan):
    root = Path(plan["root"])
    for source in plan["sources"]:
        if file_hash(root / source["manifest_path"]) != source["manifest_sha256"]:
            raise ValueError(f"Base manifest changed: {source['dataset_id']}")
    if implementation() != plan["implementation"] or dependencies() != plan["dependencies"]:
        raise ValueError("Implementation or dependencies changed; do not mix checkpoints")


def fingerprint(row, profile_id):
    return digest(
        [
            "speaker-structure-input-v1",
            row["sample_id"],
            row["audio_sha256"],
            "full_original_bytes",
            profile_id,
        ]
    )


def checkpoint_path(work, task):
    return Path(work) / "checkpoints" / task["dataset_id"] / f"{task['fragment_id']:06d}.parquet"


def load_checkpoint(work, task, plan_hash):
    path = checkpoint_path(work, task)
    receipt = path.with_suffix(".json")
    if not receipt.exists():
        return None
    value = json.loads(receipt.read_text())
    if value["plan_sha256"] != plan_hash or value["sha256"] != file_hash(path):
        raise ValueError(f"Checkpoint integrity failure: {path}")
    table = pq.read_table(path)
    if len(table) != task["rows"]:
        raise ValueError(f"Checkpoint row count mismatch: {path}")
    return table


def write_checkpoint(work, task, rows, plan_hash, round_number):
    table = pa.Table.from_pylist(rows, schema=checkpoint_schema())
    path = checkpoint_path(work, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(table, temporary, compression="zstd")
    if not pq.read_table(temporary).equals(table):
        raise ValueError("Checkpoint roundtrip mismatch")
    temporary.replace(path)
    counts = Counter(r["status"] for r in rows)
    write_json(
        path.with_suffix(".json"),
        dict(
            fragment_id=task["fragment_id"],
            rows=len(rows),
            plan_sha256=plan_hash,
            sha256=file_hash(path),
            round=round_number,
            counts=dict(counts),
            error_codes=dict(Counter(r["error_code"] for r in rows if r["error_code"])),
        ),
    )
    return counts


def fragment_rows(ds, task, keep):
    """Base rows of a fragment, in order; rows already ok in `keep` are yielded without audio."""
    for batch in ds.get_fragment(task["fragment_id"]).to_batches(columns=COLUMNS, batch_size=256):
        for row in batch.to_pylist():
            if row["sample_id"] in keep:
                yield row["sample_id"], None
            else:
                audio = row.pop("audio")
                row.update(audio_name=audio["path"], audio_bytes=audio["bytes"])
                yield row["sample_id"], row


def run(work):
    work = Path(work).resolve()
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return _run(work)
        except BaseException as exc:
            event(work, "stopped", error_type=type(exc).__name__, error=str(exc)[:500])
            raise


def _run(work):
    plan = json.loads((work / "plan.json").read_text())
    plan_hash = file_hash(work / "plan.json")
    check_inputs(plan)
    rounds = work / "rounds.json"
    history = json.loads(rounds.read_text()) if rounds.exists() else []
    round_number = len(history) + 1
    history.append(dict(round=round_number, started_at=datetime.now(BEIJING).isoformat()))
    write_json(rounds, history)

    def log(phase, **fields):
        event(work, phase, round=round_number, **fields)

    service = Service(plan["profile"], log)
    service.check_identity(force=True)
    concurrency = plan["profile"]["request"]["concurrency"]
    root = Path(plan["root"])
    totals = Counter()
    with ThreadPoolExecutor(concurrency) as pool:
        for source in plan["sources"]:  # One dataset at a time.
            name = source["dataset_id"]
            ds = lance.dataset(root / source["table_path"], version=source["lance_version"])
            tasks = [t for t in plan["tasks"] if t["dataset_id"] == name]
            progress = dict(started=time.time(), requested=0, counts=Counter())
            log("dataset_started", dataset=name, fragments=len(tasks))
            # Requests stream across fragment boundaries; a fragment's checkpoint is written as
            # soon as its last request returns, so a slow request never idles the pool.
            open_fragments, pending = {}, {}

            def finish(fragment):
                state = open_fragments.pop(fragment)
                rows = []
                for sample_id in state["order"]:
                    if sample_id in state["kept"]:
                        rows.append(state["kept"][sample_id])
                        continue
                    (status, code, result, raw, attempts), audio_sha256 = state["results"][
                        sample_id
                    ]
                    rows.append(
                        dict(
                            sample_id=sample_id,
                            input_fingerprint=fingerprint(
                                dict(sample_id=sample_id, audio_sha256=audio_sha256),
                                plan["profile_id"],
                            ),
                            status=status,
                            error_code=code,
                            result=result,
                            round=round_number,
                            attempts=attempts,
                            raw_response=raw,
                        )
                    )
                counts = write_checkpoint(work, state["task"], rows, plan_hash, round_number)
                progress["counts"].update(counts)
                progress["requested"] += len(state["results"])
                elapsed = time.time() - progress["started"]
                log(
                    "progress",
                    dataset=name,
                    fragment=fragment,
                    requested=progress["requested"],
                    requests_per_second=round(progress["requested"] / max(elapsed, 0.001), 2),
                    counts=dict(progress["counts"]),
                )

            def collect(futures):
                for future in futures:
                    fragment, sample_id, audio_sha256 = pending.pop(future)
                    state = open_fragments[fragment]
                    state["results"][sample_id] = (future.result(), audio_sha256)
                    state["remaining"] -= 1
                    if state["remaining"] == 0 and state["submitted"]:
                        finish(fragment)

            for task in tasks:
                previous = load_checkpoint(work, task, plan_hash)
                kept = {}
                if previous is not None:
                    for row in previous.to_pylist():
                        if row["status"] == "ok":
                            kept[row["sample_id"]] = row
                if previous is not None and len(kept) == task["rows"]:
                    progress["counts"]["ok"] += task["rows"]
                    continue
                service.check_identity()
                fragment = task["fragment_id"]
                state = dict(
                    task=task, order=[], kept=kept, results={}, remaining=0, submitted=False
                )
                open_fragments[fragment] = state
                for sample_id, row in fragment_rows(ds, task, kept):
                    state["order"].append(sample_id)
                    if row is None:
                        continue
                    while len(pending) >= 2 * concurrency:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
                    future = pool.submit(service.request, row)
                    pending[future] = (fragment, sample_id, row["audio_sha256"])
                    state["remaining"] += 1
                state["submitted"] = True
                if state["remaining"] == 0:
                    finish(fragment)
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
            if open_fragments:
                raise RuntimeError(f"Fragments left unfinished: {sorted(open_fragments)}")
            totals.update(progress["counts"])
            log("dataset_round_complete", dataset=name, counts=dict(progress["counts"]))
    history[-1].update(finished_at=datetime.now(BEIJING).isoformat(), counts=dict(totals))
    write_json(rounds, history)
    log("round_complete", counts=dict(totals))
    return dict(totals)


def publish(work):
    work = Path(work).resolve()
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((work / "plan.json").read_text())
        plan_hash = file_hash(work / "plan.json")
        check_inputs(plan)
        outputs = []
        for source in plan["sources"]:
            tasks = [t for t in plan["tasks"] if t["dataset_id"] == source["dataset_id"]]
            counts, errors = Counter(), Counter()
            for task in tasks:
                table = load_checkpoint(work, task, plan_hash)
                if table is None:
                    raise ValueError(f"Fragment never ran: {task}")
                counts.update(table["status"].to_pylist())
                errors.update(e for e in table["error_code"].to_pylist() if e)

            def batches(tasks=tasks):
                names = ["sample_id", "input_fingerprint", "status", "error_code", "result"]
                for task in tasks:
                    table = pq.read_table(checkpoint_path(work, task), columns=names)
                    yield from table.to_batches(max_chunksize=4096)

            event(work, "publishing", dataset=source["dataset_id"], counts=dict(counts))
            outputs.append(
                publish_output(
                    plan["root"], source, plan["run_id"], work, batches, counts, dict(errors)
                )
            )
        check_inputs(plan)
        manifest = dict(
            artifact_kind="annotation",
            contract_version="v0.1",
            schema_version="speaker-structure-moss-v1",
            status="complete",
            task=TASK,
            target_kind="sample",
            run_id=plan["run_id"],
            profile=plan["profile"],
            profile_id=plan["profile_id"],
            inputs=plan["sources"],
            outputs=outputs,
            finished_at=datetime.now(BEIJING).isoformat(),
            code_sha256=plan["implementation"],
            dependencies=plan["dependencies"],
            execution=dict(
                kind="external_service_inference",
                plan_sha256=plan_hash,
                rounds=json.loads((work / "rounds.json").read_text()),
                selection_scope="all_base_samples",
                raw_responses="private checkpoints, not published",
            ),
        )
        path = Path(plan["root"]) / "annotations" / TASK / plan["run_id"] / "manifest.json"
        if path.exists():
            old = json.loads(path.read_text())
            if old["execution"]["plan_sha256"] != plan_hash or old["outputs"] != outputs:
                raise ValueError("Published run differs from this publication")
        else:
            write_json(path, manifest)
        event(work, "complete", manifest=str(path))
        return path
