"""Shared selection metadata semantics for text and codec publication."""

from pathlib import Path

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


def selected_metadata(row, text_sources, raw_revision=None):
    """Selected text/language for one target.

    Annotation sources (code >= 1) need the pinned raw revision: the selected text must be
    its normalized form, and text_revision binds the raw value (spec 05 selected-text-v1).
    """
    replacement = row["selected_text"]
    code = row["selected_text_source"]
    if (replacement is None) != (code is None):
        raise ValueError("Incomplete selected text override")
    code = 0 if code is None else code
    if str(code) not in text_sources:
        raise ValueError("Unknown selected text source")
    source = text_sources[str(code)]
    if code == 0:
        raw = row["text"]
    else:
        if source.get("kind") != "annotation" or not isinstance(raw_revision, str):
            raise ValueError("Annotation text requires its pinned raw revision")
        if source["normalization"] != "python.str.strip" or raw_revision.strip() != replacement:
            raise ValueError("Selected text is not the normalized annotation revision")
        raw = raw_revision
    text = row["text"] if replacement is None else replacement
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Supervised codec target has no selected text")
    result = {
        k: row[k] for k in ("dataset_id", "source_key", "text_kind", "speaker_id", "speaker_scope")
    }
    result.update(
        text=text,
        language=row["language"] if row["selected_language"] is None else row["selected_language"],
        text_source=code,
        text_revision=digest(["selected-text-v1", raw, source["normalization"]]),
    )
    return result


def annotation_revisions(root, selection, dataset_id):
    """Return fetch(ids) -> {sample_id: raw annotation text} from the selection's pinned table."""
    import lance

    inputs = [a for a in selection.get("annotation_inputs", []) if a["dataset_id"] == dataset_id]
    if not inputs:
        return None
    if len(inputs) != 1 or inputs[0].get("branch") is not None:
        raise ValueError("Expected one pinned main-table annotation input per dataset")
    pinned = inputs[0]
    table = lance.dataset(Path(root) / pinned["table_path"], version=pinned["lance_version"])

    def fetch(ids):
        quoted = ",".join("'" + i + "'" for i in ids)
        found = table.to_table(
            columns=["sample_id", "status", "result"], filter=f"sample_id IN ({quoted})"
        ).to_pylist()
        texts = {r["sample_id"]: r["result"]["text"] for r in found if r["status"] == "ok"}
        if set(texts) != set(ids):
            raise ValueError("Pinned annotation revision missing for a selected target")
        return pinned["text_source"], texts

    return fetch
