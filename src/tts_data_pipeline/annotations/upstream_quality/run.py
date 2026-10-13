"""Import the Emilia family's upstream DNSMOS into upstream_quality sample tables (spec 05).

prepare pins every base input and the task plan; run converts each base fragment into a
verified checkpoint, then publishes one results.lance per dataset and the global manifest.
"""

import fcntl
import hashlib
import json
import math
import multiprocessing
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import lance
import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from ...feature_runtime import event, file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from .publication import TASK, publish_output
from .schema import annotation_schema

PROFILE = "upstream-dnsmos-p835-ovrl-v1"
COLUMNS = ["sample_id", "audio_sha256", "language", "metadata_json"]
EXPECTED_RANGE = (1.0, 5.0)
SOURCES = {
    "emilia": dict(
        field_path=["upstream", "dnsmos"],
        granularity="utterance",
        upstream_filter=dict(all_languages=3.0),
    ),
    "emilia_yodas": dict(
        field_path=["upstream", "dnsmos"],
        granularity="utterance",
        upstream_filter=dict(all_languages=2.4),
    ),
    "emilia2": dict(
        field_path=["upstream", "short", "dnsmos"],
        granularity="short_clip",
        upstream_filter=dict(en=3.0, zh=3.0, other_languages=2.4),
    ),
}
_PLAN = None
_TABLES = None


def implementation():
    folder = Path(__file__).parent
    return {name: file_hash(folder / name) for name in ("run.py", "schema.py", "publication.py")}


def profile(datasets):
    return dict(
        task=TASK,
        name=PROFILE,
        execution_kind="upstream_metadata_import",
        metric=dict(
            name="dnsmos_p835_ovrl_calibrated",
            model="DNSMOS P.835",
            model_file="sig_bak_ovr.onnx",
            model_source="microsoft/DNS-Challenge DNSMOS/sig_bak_ovr.onnx",
            model_file_sha256=None,
            runtime="onnxruntime",
            personalized=False,
            calibration="upstream_polynomial",
            retained_outputs=["OVRL"],
            expected_range=list(EXPECTED_RANGE),
            direction="higher_is_better",
            input_extension="upstream: official DNSMOS repeats audio shorter than 9 s; "
            "not verifiable per row",
        ),
        model_attribution="user_provided",
        sources={
            name: dict(
                **{k: SOURCES[name][k] for k in ("field_path", "granularity")},
                upstream_filter=dict(
                    thresholds=SOURCES[name]["upstream_filter"],
                    role="upstream_documentation_only",
                    derivation="data owner statement; hard cut observed in this data "
                    "(docs/upstream-dnsmos-report.md)",
                ),
            )
            for name in datasets
        },
        parsing=dict(
            parser="orjson",
            parser_version=orjson.__version__,
            missing="skipped/upstream_missing",
            invalid="failed/upstream_invalid",
            bool_rejected_before_number=True,
            out_of_expected_range="ok, counted per dataset",
        ),
        input_fingerprint="upstream-quality-input-v1",
    )


def check_inputs(plan):
    root = Path(plan["root"])
    for source in plan["sources"]:
        if file_hash(root / source["manifest_path"]) != source["manifest_sha256"]:
            raise ValueError(f"Base manifest changed: {source['dataset_id']}")
    if implementation() != plan["implementation"]:
        raise ValueError("Importer implementation changed; do not mix checkpoint implementations")
    if orjson.__version__ != plan["profile"]["parsing"]["parser_version"]:
        raise ValueError("Parser version changed")


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
        ids = batch.column(0)
        if ids.null_count:
            raise ValueError("Null base sample identity")
        checksum.update(b"".join(s.encode("ascii") for s in ids.to_pylist()))
        rows += len(ids)
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
    if sum(t["rows"] for t in tasks) != rows:
        raise ValueError(f"Fragment coverage mismatch: {name}")
    return source, tasks


def prepare(root, work, datasets=tuple(SOURCES)):
    root, work = Path(root).resolve(), Path(work).resolve()
    if work.exists():
        raise FileExistsError("Use a new work directory; run resumes a prepared plan")
    unknown = set(datasets) - set(SOURCES)
    if unknown:
        raise ValueError(f"No upstream DNSMOS source defined for {sorted(unknown)}")
    sources, tasks = [], []
    for name in datasets:
        source, dataset_tasks = pin_source(root, name)
        sources.append(source)
        tasks += dataset_tasks
    prof = profile(datasets)
    now = datetime.now(BEIJING)
    plan = dict(
        root=str(root),
        sources=sources,
        profile=prof,
        profile_id=digest(prof),
        implementation=implementation(),
        run_id=f"tts-ann-{TASK}-" + now.strftime("%Y%m%dT%H%M%Sbjt") + "-01",
        created_at=now.isoformat(),
        tasks=tasks,
    )
    check_inputs(plan)
    work.mkdir(parents=True)
    write_json(work / "plan.json", plan)
    event(work, "prepared", rows=sum(s["rows"] for s in sources), fragments=len(tasks))
    return plan


def read_score(metadata_json, field_path):
    """(status, error_code, value) for one row under the pinned parsing rule."""
    try:
        node = orjson.loads(metadata_json)
    except orjson.JSONDecodeError:
        return "failed", "upstream_invalid", None
    for key in field_path:
        if not isinstance(node, dict):
            return "failed", "upstream_invalid", None
        node = node.get(key)
        if node is None:
            return "skipped", "upstream_missing", None
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return "failed", "upstream_invalid", None
    try:
        value = float(node)
    except OverflowError:
        return "failed", "upstream_invalid", None
    if not math.isfinite(value):
        return "failed", "upstream_invalid", None
    return "ok", None, value


def convert_row(dataset, base, profile_id):
    field_path = SOURCES[dataset]["field_path"]
    metadata_json = base["metadata_json"]
    fingerprint = digest(
        [
            "upstream-quality-input-v1",
            base["sample_id"],
            base["audio_sha256"],
            dataset,
            ".".join(field_path),
            hashlib.sha256(metadata_json.encode("utf-8")).hexdigest(),
            profile_id,
        ]
    )
    status, error_code, value = read_score(metadata_json, field_path)
    return dict(
        sample_id=base["sample_id"],
        input_fingerprint=fingerprint,
        status=status,
        error_code=error_code,
        result=dict(audio_sha256=base["audio_sha256"], upstream_dnsmos=value)
        if status == "ok"
        else None,
    )


def _initialize(plan):
    global _PLAN, _TABLES
    _PLAN = plan
    root = Path(plan["root"])
    _TABLES = {
        s["dataset_id"]: lance.dataset(root / s["table_path"], version=s["lance_version"])
        for s in plan["sources"]
    }


def _checkpoint(work, task):
    return Path(work) / "checkpoints" / task["dataset_id"] / f"{task['fragment_id']:06d}.parquet"


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
    dataset = task["dataset_id"]
    table = _TABLES[dataset].get_fragment(task["fragment_id"]).to_table(columns=COLUMNS)
    if len(table) != task["rows"]:
        raise ValueError("Fragment row count changed")
    low, high = EXPECTED_RANGE
    outputs, counts, errors = [], Counter(), Counter()
    total, minimum, maximum, out_of_range = 0.0, None, None, 0
    for base in table.to_pylist():
        row = convert_row(dataset, base, _PLAN["profile_id"])
        outputs.append(row)
        counts[row["status"]] += 1
        if row["error_code"]:
            errors[row["error_code"]] += 1
        if row["status"] == "ok":
            value = row["result"]["upstream_dnsmos"]
            total += value
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
            out_of_range += not low <= value <= high
    result = pa.Table.from_pylist(outputs, schema=annotation_schema())
    path = _checkpoint(work, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    pq.write_table(result, temporary, compression="zstd")
    if not pq.read_table(temporary).equals(result):
        raise ValueError("Checkpoint roundtrip mismatch")
    temporary.replace(path)
    receipt = dict(
        dataset_id=dataset,
        fragment_id=task["fragment_id"],
        rows=len(result),
        plan_sha256=plan_hash,
        sha256=file_hash(path),
        counts=dict(counts),
        error_codes=dict(errors),
        score_sum=total,
        score_min=minimum,
        score_max=maximum,
        out_of_expected_range=out_of_range,
        elapsed_seconds=time.monotonic() - started,
    )
    write_json(path.with_suffix(".json"), receipt)
    return receipt


def summarize(receipts):
    """Per-dataset status counts and score statistics from fragment receipts."""
    out = {}
    for r in receipts:
        s = out.setdefault(
            r["dataset_id"],
            dict(counts=Counter(), error_codes=Counter(), sum=0.0, min=None, max=None, oor=0),
        )
        s["counts"].update(r["counts"])
        s["error_codes"].update(r["error_codes"])
        s["sum"] += r["score_sum"]
        s["oor"] += r["out_of_expected_range"]
        for key, pick in (("min", min), ("max", max)):
            value = r[f"score_{key}"]
            if value is not None:
                s[key] = value if s[key] is None else pick(s[key], value)
    return {
        name: dict(
            counts=dict(s["counts"]),
            error_codes=dict(s["error_codes"]),
            statistics=dict(
                ok_rows=s["counts"]["ok"],
                upstream_dnsmos_min=s["min"],
                upstream_dnsmos_max=s["max"],
                upstream_dnsmos_mean=s["sum"] / s["counts"]["ok"] if s["counts"]["ok"] else None,
                out_of_expected_range=s["oor"],
            ),
        )
        for name, s in out.items()
    }


def run(work, workers=8, *, publish=True, allow_non_ok=False):
    work = Path(work).resolve()
    if workers < 1:
        raise ValueError("workers must be positive")
    with (work / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return _run(work, workers, publish, allow_non_ok)
        except Exception as exc:
            event(work, "failed", error_type=type(exc).__name__, error=str(exc))
            raise


def _run(work, workers, publish, allow_non_ok):
    plan = json.loads((work / "plan.json").read_text())
    plan_hash = file_hash(work / "plan.json")
    check_inputs(plan)
    total_rows = sum(s["rows"] for s in plan["sources"])
    pending, receipts = [], []
    for task in plan["tasks"]:
        receipt = verified_checkpoint(work, task, plan_hash)
        if receipt:
            receipts.append(receipt)
        else:
            pending.append(task)
    started, done = time.monotonic(), sum(r["rows"] for r in receipts)
    event(work, "importing", completed_rows=done, total_rows=total_rows, workers=workers)
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
                for other in futures:
                    other.cancel()
                raise
            receipts.append(receipt)
            elapsed = time.monotonic() - started
            event(
                work,
                "importing",
                completed_rows=sum(r["rows"] for r in receipts),
                total_rows=total_rows,
                completed_fragments=len(receipts),
                total_fragments=len(plan["tasks"]),
                rows_per_second=(sum(r["rows"] for r in receipts) - done) / max(elapsed, 0.001),
            )
    summary = summarize(receipts)
    for source in plan["sources"]:
        counts = summary.get(source["dataset_id"], {}).get("counts", {})
        if sum(counts.values()) != source["rows"]:
            raise ValueError(f"Checkpoint coverage mismatch: {source['dataset_id']}")
    check_inputs(plan)
    write_json(work / "validation.json", dict(summary=summary, all_inputs_unchanged=True))
    non_ok = {n: s["counts"] for n, s in summary.items() if set(s["counts"]) - {"ok"}}
    if not publish:
        event(work, "checkpoints_complete", summary=summary)
        return None
    if non_ok and not allow_non_ok:
        raise ValueError(f"Non-ok rows need review before publication: {non_ok}")

    outputs = []
    for source in plan["sources"]:
        tasks = [t for t in plan["tasks"] if t["dataset_id"] == source["dataset_id"]]

        def batches(tasks=tasks):
            for task in tasks:
                yield from pq.read_table(_checkpoint(work, task)).to_batches(max_chunksize=4096)

        event(work, "publishing", dataset=source["dataset_id"], rows=source["rows"])
        info = summary[source["dataset_id"]]
        outputs.append(
            publish_output(
                plan["root"],
                source,
                plan["run_id"],
                work,
                batches,
                Counter(info["counts"]),
                dict(**info["statistics"], error_codes=info["error_codes"]),
            )
        )
    check_inputs(plan)
    manifest = dict(
        artifact_kind="annotation",
        contract_version="v0.1",
        schema_version="upstream-quality-dnsmos-v1",
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
        execution=dict(
            kind="upstream_metadata_import",
            workers=workers,
            plan_sha256=plan_hash,
            selection_scope="all_base_samples",
        ),
    )
    path = Path(plan["root"]) / "annotations" / TASK / plan["run_id"] / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old["execution"]["plan_sha256"] != plan_hash or old["outputs"] != outputs:
            raise ValueError("Published run differs from this import")
    else:
        write_json(path, manifest)
    event(work, "complete", manifest=str(path))
    return path
