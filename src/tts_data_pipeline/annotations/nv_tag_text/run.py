"""nv_tag_text: NVSpeech-style canonical paralinguistic text as a text revision (spec 05).

One sample_table row per base sample: ok rows carry {action keep|replace, text} plus the
source and target tags of nv-canonical-v1; rows the mapping cannot use are skipped with a
reason (missing_text, span_only_annotation) so a selection excludes them as 1003. The base is
small enough to convert in one process; publication reuses the upstream_quality writer.
"""

import hashlib
import importlib.metadata
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa

from ...contract import annotation_sample_schema
from ...feature_runtime import event, file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from ..upstream_quality.publication import publish_output
from ..upstream_quality.run import pin_source
from . import mapping

TASK = "nv_tag_text"
SCHEMA_VERSION = "nv-tag-text-v1"
DATASETS = tuple(mapping.CONVERTERS)
COLUMNS = ["sample_id", "audio_sha256", "language", "text", "metadata_json"]


def result_type():
    strings = pa.list_(pa.string())
    return pa.struct(
        [
            pa.field("action", pa.string()),
            pa.field("text", pa.string()),
            pa.field("audio_sha256", pa.string()),
            pa.field("source_language", pa.string()),
            pa.field("source_tags", strings),
            pa.field("target_tags", strings),
            pa.field("extended", pa.bool_()),
            pa.field("fixes", strings),
        ]
    )


def annotation_schema():
    return annotation_sample_schema(result_type())


def implementation():
    folder = Path(__file__).parent
    return {name: file_hash(folder / name) for name in ("run.py", "mapping.py")}


def dependencies():
    return {name: importlib.metadata.version(name) for name in ("pylance", "pyarrow")}


def profile(datasets):
    return dict(
        task=TASK,
        name=mapping.VERSION,
        execution_kind="deterministic_text_mapping",
        mapping=mapping.definition(),
        datasets=list(datasets),
        result="text revision {action, text}: keep when the canonical text equals base text",
        skipped=dict(
            missing_text="base text empty",
            span_only_annotation="smiip_nv clip whose only annotation is span style "
            "(<x/>...</x>, speech produced while laughing/crying, not a point event)",
        ),
        input_fingerprint="nv-tag-text-input-v1",
    )


def convert_row(dataset, base, profile_id):
    text = base["text"]
    fingerprint = digest(
        [
            "nv-tag-text-input-v1",
            base["sample_id"],
            base["audio_sha256"],
            dataset,
            hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
            profile_id,
        ]
    )
    reason = None
    if text is None or not text.strip():
        reason = "missing_text"
    elif dataset == "smiip_nv" and json.loads(base["metadata_json"])["text_style"] == "span":
        reason = "span_only_annotation"
    row = dict(sample_id=base["sample_id"], input_fingerprint=fingerprint)
    if reason:
        return dict(row, status="skipped", error_code=reason, result=None)
    canonical, info = mapping.CONVERTERS[dataset](text)
    if not canonical:
        raise ValueError(f"Canonical text is empty: {dataset} {base['sample_id']}")
    keep = canonical == text
    return dict(
        row,
        status="ok",
        error_code=None,
        result=dict(
            action="keep" if keep else "replace",
            text=None if keep else canonical,
            audio_sha256=base["audio_sha256"],
            source_language=base["language"],
            **info,
        ),
    )


def summarize(rows):
    counts, errors, actions, tags, fixes = Counter(), Counter(), Counter(), Counter(), Counter()
    extended = 0
    for row in rows:
        counts[row["status"]] += 1
        if row["error_code"]:
            errors[row["error_code"]] += 1
        result = row["result"]
        if result:
            actions[result["action"]] += 1
            tags.update(result["target_tags"])
            fixes.update(result["fixes"])
            extended += result["extended"]
    return counts, dict(
        error_codes=dict(errors),
        actions=dict(actions),
        target_tags=dict(tags.most_common()),
        extended_rows=extended,
        fixes=dict(fixes),
    )


def run(root, work, datasets=DATASETS):
    """Convert, publish each dataset's table and then the global manifest."""
    root, work = Path(root).resolve(), Path(work).resolve()
    unknown = set(datasets) - set(DATASETS)
    if unknown:
        raise ValueError(f"No nv-canonical mapping for {sorted(unknown)}")
    plan_path = work / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        if plan["implementation"] != implementation() or plan["datasets"] != list(datasets):
            raise ValueError("Resume needs the same implementation and datasets")
    else:
        now = datetime.now(BEIJING)
        prof = profile(datasets)
        plan = dict(
            root=str(root),
            datasets=list(datasets),
            sources=[pin_source(root, name)[0] for name in datasets],
            profile=prof,
            profile_id=digest(prof),
            implementation=implementation(),
            dependencies=dependencies(),
            run_id=f"tts-ann-{TASK}-" + now.strftime("%Y%m%dT%H%M%Sbjt") + "-01",
            created_at=now.isoformat(),
        )
        work.mkdir(parents=True)
        write_json(plan_path, plan)
    plan_hash = file_hash(plan_path)
    outputs = []
    schema = annotation_schema()
    for source in plan["sources"]:
        name = source["dataset_id"]
        if file_hash(root / source["manifest_path"]) != source["manifest_sha256"]:
            raise ValueError(f"Base manifest changed: {name}")
        base = lance.dataset(root / source["table_path"], version=source["lance_version"])
        rows = [
            convert_row(name, r, plan["profile_id"])
            for r in base.to_table(columns=COLUMNS, scan_in_order=True).to_pylist()
        ]
        table = pa.Table.from_pylist(rows, schema=schema)
        counts, stats = summarize(rows)
        event(work, "publishing", dataset=name, rows=len(rows), counts=dict(counts))
        outputs.append(
            publish_output(
                root,
                source,
                plan["run_id"],
                work,
                lambda table=table: table.to_batches(max_chunksize=4096),
                counts,
                stats,
                task=TASK,
                schema=schema,
            )
        )
    manifest = dict(
        artifact_kind="annotation",
        contract_version="v0.1",
        schema_version=SCHEMA_VERSION,
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
            kind="deterministic_text_mapping",
            plan_sha256=plan_hash,
            selection_scope="all_base_samples",
        ),
    )
    path = root / "annotations" / TASK / plan["run_id"] / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old["execution"]["plan_sha256"] != plan_hash or old["outputs"] != outputs:
            raise ValueError("Published run differs from this conversion")
    else:
        write_json(path, manifest)
    event(work, "complete", manifest=str(path))
    return path
