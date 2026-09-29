"""Legacy audit compatibility must not invent evidence or mutate release hashes."""

import hashlib
import json

import pytest

from tts_data_pipeline.manifests import read_base_manifest


def test_legacy_missing_audit_values_remain_unknown_and_file_unchanged(tmp_path):
    path = tmp_path / "manifest.json"
    raw = b'{"artifact_kind":"base","rows":12,"code_sha256":{"old.py":"hash"}}\n'
    path.write_bytes(raw)
    result = read_base_manifest(tmp_path)
    for key in (
        "rejected_rows",
        "excluded_source_records",
        "finalization",
        "checkpoint_code_versions",
        "code_migration",
    ):
        assert key in result and result[key] is None
    assert result["code_sha256"] == {"old.py": "hash"}
    assert path.read_bytes() == raw
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(raw).digest()


def test_explicit_record_exclusions_support_count_without_rewriting(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"artifact_kind": "base", "excluded_source_records": [{"row": 3}]}))
    assert read_base_manifest(tmp_path)["rejected_rows"] == 1
    assert "rejected_rows" not in json.loads(path.read_text())


def test_modern_audit_fields_are_preserved(tmp_path):
    manifest = {
        "artifact_kind": "base",
        "rejected_rows": 0,
        "excluded_source_records": [],
        "finalization": {"phase_seconds": {"total": 4.5}},
        "checkpoint_code_versions": {"old": {"a.py": "1"}, "new": {"a.py": "2"}},
        "code_migration": {"reason": "explicit recovery"},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert read_base_manifest(tmp_path) == manifest


def test_base_reader_rejects_feature_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text('{"artifact_kind":"feature"}')
    with pytest.raises(ValueError, match="base"):
        read_base_manifest(tmp_path)
