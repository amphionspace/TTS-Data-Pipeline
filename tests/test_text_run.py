import json
from pathlib import Path

import lance
import pytest
from test_codec_run import TEXT_SOURCES, fixture

from tts_data_pipeline.feature_runtime import file_hash, write_json
from tts_data_pipeline.text import run


def plan(tmp_path):
    rows, p, d = fixture(tmp_path)
    write_json(Path(p["root"]) / "manifest.json", {"fixture": True})
    p["inputs"][0]["manifest_sha256"] = file_hash(Path(p["root"]) / "manifest.json")
    write_json(
        p["selection_path"],
        dict(status="complete", text_sources=TEXT_SOURCES, outputs=[d["source"]]),
    )
    p["selection_sha256"] = file_hash(p["selection_path"])
    p["datasets"] = [d]
    path = tmp_path / "targets.json"
    write_json(path, p)
    return rows, run.prepare(path, tmp_path / "text-work", output_root=tmp_path / "output")


def test_text_resume_publish_and_idempotence(tmp_path):
    rows, p = plan(tmp_path)
    d = p["datasets"][0]
    assert not run.process_dataset(p, d, max_tasks=1)
    assert not Path(d["final"]).exists()
    checkpoint = next((Path(d["state"]) / "checkpoints").glob("*.json"))
    original = checkpoint.read_bytes()
    assert run.process_dataset(p, d)
    assert checkpoint.read_bytes() == original
    assert run.process_dataset(p, d)
    manifest = json.loads((Path(d["final"]) / "manifest.json").read_text())
    table = lance.dataset(
        Path(d["final"]) / "features.lance", version=manifest["lance_version"]
    ).to_table()
    actual = table.to_pylist()
    assert [r["target_id"] for r in actual] == [r["sample_id"] for r in rows[::2]]
    assert [r["text"] for r in actual] == [str(i) for i in range(0, 9, 2)]
    assert all(r["language"] == "en" and r["release_id"] == "v0.1" for r in actual)
    assert [r["audio_sha256"] for r in actual] == [r["audio_sha256"] for r in rows[::2]]
    assert manifest["coverage"]["ok"] == 5
    assert "audio" not in table.schema.names and "text_tokens" not in table.schema.names


def test_text_rejects_corrupted_checkpoint(tmp_path):
    _, p = plan(tmp_path)
    d = p["datasets"][0]
    run.process_dataset(p, d, max_tasks=1)
    checkpoint = json.loads(next((Path(d["state"]) / "checkpoints").glob("*.json")).read_text())
    path = Path(d["stage"]) / "features.lance/data" / checkpoint["files"][0]["name"]
    with path.open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="corrupted"):
        run.process_dataset(p, d)
    assert not Path(d["final"]).exists()


def test_text_rejects_wrong_target_order(tmp_path):
    _, p = plan(tmp_path)
    d = p["datasets"][0]
    d["tasks"][0]["ordered_ids_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="target set differs"):
        run.process_dataset(p, d)
    assert not Path(d["final"]).exists()
