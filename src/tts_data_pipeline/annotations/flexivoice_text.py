"""Publish FlexiVoice transcript repairs as an immutable text-revision annotation."""

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.parquet as pq

from ..feature_runtime import file_hash, write_json
from ..schema import digest
from ..timestamps import BEIJING
from .nv_tag_text.mapping import TAGS
from .nv_tag_text.run import annotation_schema, summarize
from .upstream_quality.publication import publish_output
from .upstream_quality.run import pin_source

TASK = "flexivoice_text"
PROFILE = {
    "task": TASK,
    "name": "flexivoice-text-v1",
    "remove": ["[inaudible]"],
    "replace": {"[LAUGHTER]": "[Laughter]", "[BREATHING]": "[Breathing]"},
    "skip": ["[GARBAGE]", "[garbage]"],
    "allowed_tags": sorted(TAGS),
    "normalization": "python.str.strip after literal replacements; no other text edits",
}
TAG = re.compile(r"\[([A-Za-z]+(?:-[A-Za-z]+)*)\]")


def convert(base):
    raw = base["text"] or ""
    fingerprint = digest(
        [
            TASK,
            base["sample_id"],
            base["audio_sha256"],
            hashlib.sha256(raw.encode()).hexdigest(),
            digest(PROFILE),
        ]
    )
    result = {"sample_id": base["sample_id"], "input_fingerprint": fingerprint}
    reason = "unintelligible_transcript" if any(t in raw for t in PROFILE["skip"]) else None
    text = raw
    fixes = []
    for token, replacement in {
        **dict.fromkeys(PROFILE["remove"], ""),
        **PROFILE["replace"],
    }.items():
        if token in text:
            fixes.append(token)
            text = text.replace(token, replacement)
    text = text.strip()
    if not text:
        reason = reason or "missing_text"
    if not reason and set(TAG.findall(text)) - TAGS:
        reason = "unknown_event_tag"
    if reason:
        return {**result, "status": "skipped", "error_code": reason, "result": None}
    return {
        **result,
        "status": "ok",
        "error_code": None,
        "result": {
            "action": "keep" if text == raw else "replace",
            "text": None if text == raw else text,
            "audio_sha256": base["audio_sha256"],
            "source_language": base["language"],
            "source_tags": TAG.findall(raw),
            "target_tags": TAG.findall(text),
            "extended": False,
            "fixes": fixes,
        },
    }


def run(root, work):
    root, work = Path(root).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=False)
    source = pin_source(root, "flexivoice")[0]
    stamp = datetime.now(BEIJING)
    run_id = f"tts-ann-{TASK}-{stamp:%Y%m%dT%H%M%S}bjt-01"
    plan = {
        "source": source,
        "profile": PROFILE,
        "run_id": run_id,
        "implementation_sha256": file_hash(__file__),
    }
    write_json(work / "plan.json", plan)
    base = lance.dataset(root / source["table_path"], version=source["lance_version"])
    schema = annotation_schema()
    counts, errors, actions, tags, fixes = (Counter() for _ in range(5))
    scratch = work / "rows.parquet"
    with pq.ParquetWriter(scratch, schema) as writer:
        for batch in base.scanner(
            columns=["sample_id", "audio_sha256", "text", "language"],
            scan_in_order=True,
            batch_size=8192,
        ).to_batches():
            rows = [convert(r) for r in batch.to_pylist()]
            count, stats = summarize(rows)
            counts.update(count)
            errors.update(stats["error_codes"])
            actions.update(stats["actions"])
            tags.update(stats["target_tags"])
            fixes.update(stats["fixes"])
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    stats = {
        "error_codes": dict(errors),
        "actions": dict(actions),
        "target_tags": dict(tags),
        "fixes": dict(fixes),
    }
    output = publish_output(
        root,
        source,
        run_id,
        work,
        lambda: pq.ParquetFile(scratch).iter_batches(batch_size=8192),
        counts,
        stats,
        task=TASK,
        schema=schema,
    )
    manifest = {
        "artifact_kind": "annotation",
        "contract_version": "v0.1",
        "schema_version": "nv-tag-text-v1",
        "status": "complete",
        "task": TASK,
        "target_kind": "sample",
        "run_id": run_id,
        "profile": PROFILE,
        "profile_id": digest(PROFILE),
        "inputs": [source],
        "outputs": [output],
        "execution": plan,
        "finished_at": datetime.now(BEIJING).isoformat(),
    }
    destination = root / "annotations" / TASK / run_id / "manifest.json"
    write_json(destination, manifest)
    print(
        json.dumps({"manifest": str(destination), "counts": counts, "statistics": stats}),
        flush=True,
    )
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    run(args.root, args.work)
