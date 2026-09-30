"""Resumable CPU-only publication of selected text, independent of audio extraction."""

import fcntl
import json
import multiprocessing
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
from lance.file import LanceFileReader
from lance.fragment import FragmentMetadata, write_fragments

from ..contract import schema_description, text_feature_schema
from ..feature_contract import make_feature_run_id, validate_feature_coverage
from ..feature_runtime import (
    event,
    file_hash,
    open_source,
    ordered_hash,
    published_manifest,
    verify_checkpoint,
    write_json,
)
from ..schema import digest
from ..timestamps import BEIJING
from . import metadata
from .metadata import SOURCE_COLUMNS, selected_metadata

PROFILE_NAME = "selection-text-v1"


def prepare(targets_plan, work, *, output_root=None, datasets=None):
    original = json.loads(Path(targets_plan).read_text())
    if file_hash(original["selection_path"]) != original["selection_sha256"]:
        raise ValueError("Selection manifest changed")
    selection = json.loads(Path(original["selection_path"]).read_text())
    if selection["status"] != "complete":
        raise ValueError("Selection is incomplete")
    definition = {
        "kind": "text",
        "selection_sha256": original["selection_sha256"],
        "text_sources": selection["text_sources"],
        "metadata_code_sha256": file_hash(metadata.__file__),
        "tokenization": None,
    }
    work = Path(work).resolve()
    created = datetime.now(BEIJING).isoformat()
    p = dict(
        root=original["root"],
        output_root=str(Path(output_root or original["root"]).resolve()),
        work=str(work),
        selection_path=original["selection_path"],
        selection_sha256=original["selection_sha256"],
        inputs=original["inputs"],
        profile=definition,
        profile_id=digest(definition),
        profile_name=PROFILE_NAME,
        execution_code_sha256=file_hash(__file__),
        target_plan_sha256=file_hash(targets_plan),
        datasets=[],
    )
    for source_plan in original["datasets"]:
        source = source_plan["source"]
        name = source["dataset_id"]
        if datasets is not None and name not in datasets:
            continue
        if source not in selection["outputs"]:
            raise ValueError("Target source differs from selection")
        d = {k: source_plan[k] for k in ("source", "target_count", "target_set_sha256", "tasks")}
        if (
            sum(t["rows"] for t in d["tasks"]) != d["target_count"]
            or d["target_count"] != source["selected_rows"]
        ):
            raise ValueError("Target count differs")
        # Tasks must be a contiguous partition of each selected source fragment.
        offsets = {}
        task_ids = set()
        for t in d["tasks"]:
            if (
                t["dataset"] != name
                or t["rows"] < 1
                or t["id"] in task_ids
                or t["offset"] != offsets.get(t["fragment"], 0)
            ):
                raise ValueError("Invalid target task partition")
            offsets[t["fragment"]] = t["offset"] + t["rows"]
            task_ids.add(t["id"])
        run_id = make_feature_run_id(name, "text", PROFILE_NAME, created)
        release = Path(p["output_root"]) / "datasets" / name / source["release_id"]
        final = release / "features/text" / run_id
        d.update(
            run_id=run_id,
            final=str(final),
            stage=str(final) + ".incomplete",
            state=str(release.parent / ".state" / source["release_id"] / "features/text" / run_id),
        )
        p["datasets"].append(d)
    if not p["datasets"] or (
        datasets and set(datasets) != {d["source"]["dataset_id"] for d in p["datasets"]}
    ):
        raise ValueError("Unknown or empty dataset selection")
    validate_plan(p)
    work.mkdir(parents=True, exist_ok=False)
    write_json(work / "plan.json", p)
    event(work, "planned", rows_total=sum(d["target_count"] for d in p["datasets"]))
    return p


def validate_plan(p):
    if (
        file_hash(p["selection_path"]) != p["selection_sha256"]
        or file_hash(__file__) != p["execution_code_sha256"]
        or file_hash(metadata.__file__) != p["profile"]["metadata_code_sha256"]
        or digest(p["profile"]) != p["profile_id"]
    ):
        raise ValueError("Pinned selection, profile or code changed")
    for item in p["inputs"]:
        if file_hash(Path(p["root"]) / item["manifest_path"]) != item["manifest_sha256"]:
            raise ValueError("Base manifest changed")


def make_table(rows, source, text_sources):
    result = []
    for row in rows:
        if row["dataset_id"] != source["dataset_id"]:
            raise ValueError("Source dataset identity differs")
        result.append(
            dict(
                target_id=row["sample_id"],
                release_id=source["release_id"],
                audio_sha256=row["audio_sha256"],
                **selected_metadata(row, text_sources),
            )
        )
    return pa.Table.from_pylist(result, schema=text_feature_schema())


def write_task(p, d, task, table):
    destination = Path(d["stage"]) / "features.lance"
    fragments = write_fragments(table, destination, mode="create", data_storage_version="2.2")
    files, batches = [], []
    for fragment in fragments:
        for data_file in fragment.files:
            path = destination / "data" / data_file.path
            files.append(dict(name=path.name, bytes=path.stat().st_size, sha256=file_hash(path)))
            batches.extend(LanceFileReader(str(path)).read_all().to_batches())
    if not pa.Table.from_batches(batches, schema=table.schema).equals(table):
        raise ValueError("Text fragment readback differs")
    checkpoint = dict(
        task=task,
        rows=table.num_rows,
        profile_id=p["profile_id"],
        fragments=[f.to_json() for f in fragments],
        files=files,
        payloads_validated=True,
    )
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", checkpoint)
    return checkpoint


def finalize(p, d, checkpoints):
    stage, final = Path(d["stage"]), Path(d["final"])
    destination = stage / "features.lance"
    fragments = [
        FragmentMetadata.from_json(json.dumps(f)) for c in checkpoints for f in c["fragments"]
    ]
    previous = lance.dataset(destination).version if (destination / "_versions").exists() else 0
    ds = lance.LanceDataset.commit(
        destination,
        lance.LanceOperation.Overwrite(text_feature_schema(), fragments),
        read_version=previous,
    )
    # Verify the assembled row stream against every pinned task, without a global join/sort.
    batches = ds.scanner(columns=["target_id"], scan_in_order=True).to_batches()
    ids = (sid for batch in batches for sid in batch["target_id"].to_pylist())
    for task in d["tasks"]:
        actual = [next(ids, None) for _ in range(task["rows"])]
        if None in actual or ordered_hash(actual) != task["ordered_ids_sha256"]:
            raise ValueError("Published text target order differs")
    if next(ids, None) is not None:
        raise ValueError("Extra text targets")
    ds.create_scalar_index("target_id", "BTREE")
    if d["run_id"] in ds.tags.list():
        ds.tags.delete(d["run_id"])
    ds.tags.create(d["run_id"], ds.version)
    source, count = d["source"], d["target_count"]
    base = next(i for i in p["inputs"] if i["dataset_id"] == source["dataset_id"])
    manifest = dict(
        contract_version="v0.1",
        artifact_kind="feature",
        kind="text",
        status="complete",
        dataset_id=source["dataset_id"],
        release_id=source["release_id"],
        run_id=d["run_id"],
        profile_id=p["profile_id"],
        profile_name=p["profile_name"],
        profile=p["profile"],
        target_kind="sample",
        rows=count,
        storage_format="lance",
        storage_kind="result_table",
        storage_version="2.2",
        schema_version="v0.1",
        schema_sha256=digest(schema_description(ds.schema)),
        table_path=str(
            final.relative_to(
                Path(p["output_root"]) / "datasets" / source["dataset_id"] / source["release_id"]
            )
            / "features.lance"
        ),
        lance_version=ds.version,
        input_root=p["root"],
        inputs=[
            dict(
                **{
                    k: source[k]
                    for k in ("dataset_id", "release_id", "table_path", "branch", "lance_version")
                },
                **{k: base[k] for k in ("manifest_path", "manifest_sha256")},
                alias="samples",
                artifact_kind="base",
            )
        ],
        selection=dict(
            mode="selection_branch",
            input_alias="samples",
            filter="selection_reason = 0",
            available_target_rows=source["rows"],
            target_count=count,
            target_set_sha256=d["target_set_sha256"],
            manifest_path=str(Path(p["selection_path"]).relative_to(p["root"])),
            manifest_sha256=p["selection_sha256"],
        ),
        coverage=dict(total_targets=count, ok=count, failed=0, unsupported=0, skipped=0, missing=0),
        validation=dict(
            payloads_checked=count,
            target_tasks_checked=len(checkpoints),
            full_fragment_readback=True,
            tokenization=False,
        ),
        execution=dict(code_sha256=p["execution_code_sha256"]),
        finished_at=datetime.now(BEIJING).isoformat(),
    )
    validate_feature_coverage(manifest)
    write_json(stage / "manifest.json", manifest)
    stage.rename(final)
    return manifest


def process_dataset(p, d, max_tasks=None):
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    progress = Path(p["work"]) / "progress" / f"{d['source']['dataset_id']}.json"
    if Path(d["final"]).exists():
        manifest = published_manifest(p, d)
        write_json(progress, dict(rows_done=manifest["rows"], phase="complete"))
        return True
    stage = Path(d["stage"])
    reservation = stage / "execution.json"
    owner = digest(p)
    if stage.exists() and not reservation.exists():
        raise ValueError("Staging directory has no owner")
    stage.mkdir(parents=True, exist_ok=True)
    if reservation.exists() and json.loads(reservation.read_text())["owner"] != owner:
        raise ValueError("Staging directory belongs to another plan")
    write_json(reservation, dict(owner=owner))
    source = open_source(p["root"], d["source"])
    fragment_id, fragment_rows = None, None
    checkpoints, done, written = [], 0, 0
    for task in d["tasks"]:
        c = verify_checkpoint(d, task)
        if c is not None and c["profile_id"] != p["profile_id"]:
            raise ValueError("Checkpoint profile changed")
        if c is None:
            if max_tasks is not None and written >= max_tasks:
                write_json(progress, dict(rows_done=done, phase="paused_after_limit"))
                return False
            if task["fragment"] != fragment_id:
                fragment_id = task["fragment"]
                fragment_rows = source.scanner(
                    fragments=[source.get_fragment(fragment_id)],
                    columns=[*SOURCE_COLUMNS, "audio_sha256"],
                    filter="selection_reason = 0",
                    scan_in_order=True,
                    batch_readahead=1,
                    fragment_readahead=1,
                ).to_table()
            rows = fragment_rows.slice(task["offset"], task["rows"]).to_pylist()
            if (
                len(rows) != task["rows"]
                or ordered_hash([r["sample_id"] for r in rows]) != task["ordered_ids_sha256"]
            ):
                raise ValueError("Text input target set differs")
            c = write_task(p, d, task, make_table(rows, d["source"], p["profile"]["text_sources"]))
            written += 1
        checkpoints.append(c)
        done += c["rows"]
        write_json(progress, dict(rows_done=done, phase="materializing"))
    write_json(progress, dict(rows_done=done, phase="publishing"))
    finalize(p, d, checkpoints)
    write_json(progress, dict(rows_done=done, phase="complete"))
    return True


def run(work, workers=2, max_tasks=None):
    work = Path(work)
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = json.loads((work / "plan.json").read_text())
        validate_plan(p)
        started = time.perf_counter()
        total = sum(d["target_count"] for d in p["datasets"])
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            pending = {pool.submit(process_dataset, p, d, max_tasks) for d in p["datasets"]}
            complete = True
            while pending:
                finished, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                for future in finished:
                    complete = future.result() and complete
                progress = [json.loads(f.read_text()) for f in (work / "progress").glob("*.json")]
                event(
                    work,
                    "materializing",
                    rows_done=sum(x["rows_done"] for x in progress),
                    rows_total=total,
                    workers=workers,
                    elapsed_seconds=time.perf_counter() - started,
                )
        event(
            work,
            "complete" if complete else "paused_after_limit",
            rows_done=sum(
                json.loads(f.read_text())["rows_done"] for f in (work / "progress").glob("*.json")
            ),
            rows_total=total,
            elapsed_seconds=time.perf_counter() - started,
        )
