import io
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import hifitts2
from tts_data_pipeline.bulk import bulk_convert
from tts_data_pipeline.writer import release_dataset


def fixture(root, failure=None):
    (root / "data").mkdir(parents=True)
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(2205), 22050, format="FLAC")
    rows = []
    for index, split in enumerate(["train", "dev_seen", "test_seen", "dev_unseen", "test_unseen"]):
        path = f"12/34/recording_{index}.flac"
        meta = {
            "audio_filepath": path,
            "speaker": "12",
            "set": split,
            "duration": 0.12,
            "bandwidth": 10000,
            "speaker_count": 1,
            "wer": 0.2,
            "cer": 0.1,
            "text_source": "mls",
            "text": "hello",
            "normalized_text": "Hello.",
        }
        row = dict(
            meta,
            metadata_json=json.dumps(dict(meta, extra={"keep": True})),
            audio={"path": path, "bytes": buffer.getvalue()},
        )
        rows.append(row)
    if failure == "metadata":
        rows[0]["wer"] = 0.8
    elif failure == "speaker":
        rows[0]["speaker"] = "wrong"
    elif failure == "audio_path":
        rows[0]["audio"]["path"] = "different.flac"
    pq.write_table(pa.Table.from_pylist(rows), root / "data/shard-fixture.parquet")
    return rows


def test_hifitts2_roundtrip_preserves_source_scores_audio_and_splits(tmp_path):
    root = tmp_path / "raw"
    original = fixture(root)
    out = tmp_path / "release"
    manifest = bulk_convert("hifitts2", root, out, workers=1, deep_verify=True)
    assert manifest["rows"] == 5
    rows = release_dataset(out).to_table().to_pylist()
    for row, source in zip(rows, original, strict=True):
        assert row["audio"]["bytes"] == source["audio"]["bytes"]
        assert row["sample_rate"] == 22050 and row["source_split"] == "train"
        assert row["duration_seconds"] == 0.1
        meta = json.loads(row["metadata_json"])
        assert meta["original_split"] == source["set"]
        assert meta["upstream"] == json.loads(source["metadata_json"])
        assert meta["parquet_fields"]["wer"] == 0.2
        assert row["text_variants"][0]["text"] == "Hello."
        assert json.loads(row["source_locator_json"])["root"] == "hifitts2_parquet"


@pytest.mark.parametrize("failure", ["metadata", "speaker", "audio_path"])
def test_hifitts2_rejects_conflicting_source_fields(tmp_path, failure):
    fixture(tmp_path, failure)
    with pytest.raises(ValueError, match="HiFiTTS2"):
        list(hifitts2.iter_records(tmp_path, "test"))
