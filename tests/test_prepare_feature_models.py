"""Offline tests: no model downloads or network access."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_feature_models.py"


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location("prepare_feature_models", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_exact_top_level_downloads_and_complete_hashes(module, tmp_path, monkeypatch):
    remote = [
        "config.json",
        "model.safetensors.index.json",
        "model-00001.safetensors",
        "nested/config.json",
        "nested/model.safetensors",
        "speech_tokenizer/config.json",
    ]
    calls = []

    def listing(repo, revision):
        assert (repo, revision) == module.SOURCES["codec"]
        return remote

    def download(repo, **kwargs):
        calls.append(kwargs)
        folder = kwargs["local_dir"]
        folder.mkdir(parents=True)
        for name in kwargs["allow_patterns"]:
            (folder / name).write_bytes(name.encode())
        metadata = folder / ".cache/huggingface"
        metadata.mkdir(parents=True)
        (metadata / "local.json").write_text("cache metadata")

    monkeypatch.setattr(module, "list_repo_files", listing)
    monkeypatch.setattr(module, "snapshot_download", download)
    kind, result = module.prepare(tmp_path, "codec")
    assert kind == "codec"
    assert calls[0]["allow_patterns"] == sorted(remote[:3])
    assert result["files"] == {
        name: hashlib.sha256(name.encode()).hexdigest() for name in remote[:3]
    }


def test_missing_download_does_not_publish_inventory(module, tmp_path, monkeypatch):
    monkeypatch.setattr(
        module, "list_repo_files", lambda *a, **k: ["config.json", "model.safetensors"]
    )
    monkeypatch.setattr(module, "snapshot_download", lambda *a, **k: None)
    with pytest.raises(FileNotFoundError):
        module.main(["--output", str(tmp_path)])
    assert not (tmp_path / "sources.json").exists()


def test_default_output_does_not_follow_working_directory(module, tmp_path, monkeypatch):
    expected = SCRIPT.parent.parent / "cache/feature-models"
    assert module.DEFAULT_OUTPUT == expected
    # Substitute a safe test destination; main must use its constant, not CWD.
    output = tmp_path / "repo/cache/feature-models"
    monkeypatch.setattr(module, "DEFAULT_OUTPUT", output)
    monkeypatch.chdir(tmp_path)
    roots = []

    def prepare(root, kind):
        roots.append(root)
        return kind, {"files": {}}

    monkeypatch.setattr(module, "prepare", prepare)
    module.main([])
    assert roots == [output, output]
    assert set(json.loads((output / "sources.json").read_text())) == set(module.SOURCES)
    assert not (tmp_path / "cache").exists()
