import io
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import ADAPTERS
from tts_data_pipeline.convert import convert
from tts_data_pipeline.writer import release_dataset


def corpus(tmp_path):
    root = tmp_path / "CSEMOTIONS"
    (root / "data").mkdir(parents=True)
    audio = io.BytesIO()
    sf.write(audio, np.zeros(1600), 16000, format="WAV", subtype="PCM_16")
    rows = [
        {
            "audio": {"bytes": audio.getvalue(), "path": f"{i}.wav"},
            "text": "你好",
            "speaker": "speaker-1",
            "emotion": "neutral",
        }
        for i in range(3)
    ]
    pq.write_table(pa.Table.from_pylist(rows), root / "data" / "train-00000-of-00001.parquet")
    return root


def test_shards_verified_before_publication_and_no_overwrite(tmp_path):
    root = corpus(tmp_path)
    output = tmp_path / "converted"
    result = convert("csemotions", root, output, shard_bytes=4000, deep_verify=True)
    assert result["rows"] == 3
    assert len(result["shards"]) >= 1
    assert result["validation"]["fully_decoded_audio"] == 3
    assert result["validation"]["unique_ids"] == 3
    assert result["validation"]["source_to_output_records_match"]
    assert result["source_splits"] == {"train": 3}
    assert json.loads((output / "manifest.json").read_text())["status"] == "complete"
    assert not output.with_name("converted.incomplete").exists()
    with pytest.raises(FileExistsError):
        convert("csemotions", root, output)


def test_duplicate_fails_without_publishing(tmp_path, monkeypatch):
    root = corpus(tmp_path)
    adapter = ADAPTERS["csemotions"]
    original = adapter.iter_records

    def repeated(root, snapshot, *, files):
        row = next(original(root, snapshot, files=files))
        yield row
        yield row

    monkeypatch.setattr(adapter, "iter_records", repeated)
    output = tmp_path / "converted"
    with pytest.raises(ValueError, match="Duplicate sample_id"):
        convert("csemotions", root, output)
    assert not output.exists()
    assert (tmp_path / "converted.incomplete" / "FAILED.json").is_file()


def test_config_selection_keeps_dataset_relative_locator(tmp_path):
    root = tmp_path / "LibriTTS-R"
    (root / "data" / "dev.clean").mkdir(parents=True)
    (root / "data" / "train.clean.100").mkdir()
    a = root / "data" / "dev.clean" / "dev.parquet"
    b = root / "data" / "train.clean.100" / "train.parquet"
    a.touch()
    b.touch()
    assert ADAPTERS["libritts_r"].input_files(root, "dev.clean") == [a]
    assert ADAPTERS["libritts_r"].input_files(root, "all") == [a, b]
    assert ADAPTERS["libritts_r"].input_files(root) == [a, b]
    with pytest.raises(ValueError, match="Unknown"):
        ADAPTERS["libritts_r"].input_files(root, "../train.clean.100")


def test_full_libritts_uses_train_and_preserves_original_split_metadata(tmp_path):
    root = tmp_path / "LibriTTS-R"
    audio = io.BytesIO()
    sf.write(audio, np.zeros(1600), 16000, format="WAV", subtype="PCM_16")
    for index, config in enumerate(("dev.clean", "train.clean.100")):
        folder = root / "data" / config
        folder.mkdir(parents=True)
        record = {
            "id": f"id-{index}",
            "speaker_id": f"speaker-{index}",
            "chapter_id": "chapter",
            "audio": {"bytes": audio.getvalue(), "path": "old.wav"},
            "text_original": "Hello.",
            "text_normalized": "hello",
            "path": "/old/path.wav",
        }
        pq.write_table(pa.Table.from_pylist([record]), folder / "source.parquet")
    result = convert("libritts_r", root, tmp_path / "converted")
    assert result["source_splits"] == {"train": 2}
    rows = release_dataset(tmp_path / "converted").to_table().to_pylist()
    assert len(rows) == 2
    for row in rows:
        locator = json.loads(row["source_locator_json"])
        metadata = json.loads(row["metadata_json"])
        assert row["source_split"] == "train"
        assert row["source_config"] == "all"
        assert locator["path"] == f"LibriTTS-R/data/{metadata['original_split']}/source.parquet"
