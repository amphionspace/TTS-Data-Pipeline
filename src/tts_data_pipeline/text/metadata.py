"""Shared selection metadata semantics for text and codec publication."""

from ..schema import digest

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
