import json
from pathlib import Path

import pytest

from tts_data_pipeline.writer import remove_uncommitted_data_files


def test_cleanup_removes_writer_temporary_files_and_preserves_references(tmp_path):
    for name in ["kept.lance", "orphan.lance", ".tmp-interrupted", ".tmp-retained", "notes.txt"]:
        (tmp_path / name).write_bytes(b"fixture")
    outside = tmp_path / "another-directory"
    outside.mkdir()
    (outside / "keep").write_bytes(b"outside")
    (tmp_path / ".tmp-symlink").symlink_to(outside / "keep")
    removed = remove_uncommitted_data_files(tmp_path, ["data/kept.lance", ".tmp-retained"])
    assert {r["name"] for r in removed} == {"orphan.lance", ".tmp-interrupted"}
    assert sum(r["bytes"] for r in removed) == 14
    for name in ["kept.lance", ".tmp-retained", "notes.txt", ".tmp-symlink"]:
        assert (tmp_path / name).read_bytes() in {b"fixture", b"outside"}


def test_cleanup_records_intent_if_unlink_fails(tmp_path, monkeypatch):
    orphan = tmp_path / ".tmp-failure"
    orphan.write_bytes(b"keep")
    audit = tmp_path / "cleanup.jsonl"
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == orphan:
            raise PermissionError("simulated unlink failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError):
        remove_uncommitted_data_files(tmp_path, [], audit_path=audit)
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    assert orphan.exists() and [e["action"] for e in events] == ["planned"]
