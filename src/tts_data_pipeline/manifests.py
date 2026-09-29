"""Read base manifests without rewriting immutable legacy publications."""

import json
from pathlib import Path


def read_base_manifest(release):
    """Return a compatible in-memory view; hash the original file for references.

    Unknown historical audit values remain None rather than invented zeroes or
    inferred checkpoint code versions. This is not a complete schema validator.
    """
    manifest = json.loads((Path(release) / "manifest.json").read_text())
    if manifest.get("artifact_kind", "base") != "base":
        raise ValueError("Expected a base manifest")
    records = manifest.get("excluded_source_records")
    manifest.setdefault("rejected_rows", len(records) if isinstance(records, list) else None)
    manifest.setdefault("excluded_source_records", None)
    manifest.setdefault("finalization", None)
    manifest.setdefault("checkpoint_code_versions", None)
    manifest.setdefault("code_migration", None)
    return manifest
