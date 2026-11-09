import json

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import flexivoice as adapter
from tts_data_pipeline.convert import convert


def fixture(root):
    (root / "resolved_json").mkdir(parents=True)
    (root / "audio").mkdir()
    sf.write(root / "audio/test.wav", np.zeros(1600), 16000)
    rows = [
        dict(text="hello", instruction=i, wav_path="audio/test.wav", code_path="/old/x.npz")
        for i in ["soft", "loud", ""]
    ]
    (root / "resolved_json/ears.json").write_text(json.dumps(rows))
    return root / "resolved_json/ears.json"


def test_duplicate_audio_keeps_instructions_and_identity(tmp_path):
    p = fixture(tmp_path)
    rows = list(adapter.iter_records(tmp_path, "test", files=[p]))
    assert len({r["sample_id"] for r in rows}) == 3
    assert len({r["audio_sha256"] for r in rows}) == 1
    assert len({r["group_id"] for r in rows}) == 1
    assert [json.loads(r["metadata_json"])["instruction"] for r in rows] == ["soft", "loud", ""]
    assert all(r["speaker_id"] is None and r["language"] is None for r in rows)
    assert adapter.dependency_files(tmp_path, p) == [tmp_path / "audio/test.wav"]


@pytest.mark.parametrize(
    "path", ["../bad.wav", "/tmp/bad.wav", "audio/../bad.wav", "audio/missing.wav"]
)
def test_bad_audio_paths(tmp_path, path):
    with pytest.raises(ValueError):
        adapter.audio_path(tmp_path, {"wav_path": path})


def test_full_conversion_and_audio_dependency_identity(tmp_path):
    root = tmp_path / "raw"
    fixture(root)
    first = convert("flexivoice", root, tmp_path / "out1", config="ears", deep_verify=True)
    assert first["rows"] == 3 and first["status"] == "complete"
    assert first["identity_scheme"] == "source-unit-v1"
    assert len(first["inputs"][0]["semantic_dependencies"]) == 1
    sf.write(root / "audio/test.wav", np.ones(1600) * 0.1, 16000)
    second = convert("flexivoice", root, tmp_path / "out2", config="ears")
    assert first["inputs"][0]["source_snapshot"] != second["inputs"][0]["source_snapshot"]


def test_explicit_language_only():
    assert adapter.language_of("audio/emilia/EN/test.mp3") == "en"
    assert adapter.language_of("audio/instruction_data/genshin/ZH/person/test.wav") == "zh"
    assert adapter.language_of("audio/instruction_data/emotion/test.wav") is None
