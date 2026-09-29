"""Add pinned selection text to codec tables without rewriting audio codes."""

import hashlib
import json

import lance
import pyarrow as pa

from .contract import codec_text_schema
from .schema import digest

SOURCE_COLUMNS = [
    "sample_id",
    "text",
    "selected_text",
    "selected_text_source",
    "language",
    "selected_language",
    "text_kind",
    "dataset_id",
    "source_key",
    "speaker_id",
    "speaker_scope",
]


def selected_metadata(row, text_sources):
    replacement = row["selected_text"]
    code = row["selected_text_source"]
    if (replacement is None) != (code is None):
        raise ValueError("Incomplete selected text override")
    code = 0 if code is None else code
    if str(code) not in text_sources:
        raise ValueError("Unknown selected text source")
    if code != 0:
        raise ValueError(
            "Annotation text requires pinned raw revisions; unsupported by this runner"
        )
    text = row["text"] if replacement is None else replacement
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Supervised codec target has no selected text")
    raw = row["text"]
    normalization = text_sources["0"]["normalization"]
    result = {
        k: row[k] for k in ("dataset_id", "source_key", "text_kind", "speaker_id", "speaker_scope")
    }
    result.update(
        text=text,
        language=row["language"] if row["selected_language"] is None else row["selected_language"],
        text_source=code,
        text_revision=digest(["selected-text-v1", raw, normalization]),
    )
    return result


def _row_hash(checksum, target_id, metadata):
    checksum.update(
        json.dumps(
            [target_id, metadata],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def materialize_text(features, source, tasks, text_sources):
    """Verify each destination ID before adding metadata, then audit stored columns."""
    schema = codec_text_schema()
    if set(schema.names) & set(features.schema.names):
        raise ValueError("Text materialization requires a fresh unpublished codec snapshot")
    original_files = {
        f.path for fragment in features.get_fragments() for f in fragment.metadata.files
    }
    expected_hash = hashlib.sha256()
    total = 0
    pinned = lance.dataset(features.uri, version=features.version)

    def targets():
        for batch in pinned.scanner(columns=["target_id"], scan_in_order=True).to_batches():
            yield from batch.column("target_id").to_pylist()

    actual_ids = iter(targets())

    def batches():
        nonlocal total
        current_fragment, rows = None, None
        for task in tasks:
            if current_fragment != task["fragment"]:
                current_fragment = task["fragment"]
                rows = source.scanner(
                    fragments=[source.get_fragment(current_fragment)],
                    columns=SOURCE_COLUMNS,
                    filter="selection_reason = 0",
                    scan_in_order=True,
                ).to_table()
            selected = rows.slice(task["offset"], task["rows"]).to_pylist()
            ids = [r["sample_id"] for r in selected]
            if (
                len(ids) != task["rows"]
                or hashlib.sha256("".join(ids).encode()).hexdigest() != task["ordered_ids_sha256"]
            ):
                raise ValueError("Selected metadata target set changed")
            result = []
            for row in selected:
                if next(actual_ids, None) != row["sample_id"]:
                    raise ValueError("Codec/text target order differs")
                metadata = selected_metadata(row, text_sources)
                _row_hash(expected_hash, row["sample_id"], metadata)
                result.append(metadata)
                total += 1
            yield from pa.Table.from_pylist(result, schema=schema).to_batches(max_chunksize=4096)
        if next(actual_ids, None) is not None:
            raise ValueError("Codec table has extra targets")

    features.add_columns(pa.RecordBatchReader.from_batches(schema, batches()), batch_size=4096)
    actual_hash = hashlib.sha256()
    count = 0
    for batch in features.scanner(
        columns=["target_id", *schema.names], scan_in_order=True, batch_size=8192
    ).to_batches():
        for row in batch.to_pylist():
            target = row.pop("target_id")
            _row_hash(actual_hash, target, row)
            count += 1
    files = {f.path for fragment in features.get_fragments() for f in fragment.metadata.files}
    if (
        count != total
        or actual_hash.digest() != expected_hash.digest()
        or not original_files <= files
    ):
        raise ValueError("Materialized text roundtrip or original codec files changed")
    return {
        "mode": "selection_text_columns_v1",
        "columns": schema.names,
        "rows": count,
        "ordered_text_sha256": actual_hash.hexdigest(),
        "text_sources": text_sources,
        "original_codec_files_preserved": True,
    }
