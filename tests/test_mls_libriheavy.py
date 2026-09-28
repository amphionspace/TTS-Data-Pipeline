import io
import json
import tarfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import libriheavy, mls_sidon
from tts_data_pipeline.preview import preview


def audio_bytes():
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600), 16000, format="FLAC")
    return buf.getvalue()


def write_tar(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_mls_actual_member_and_language_identity(tmp_path):
    root = tmp_path / "raw"
    meta = {
        "id": "1",
        "speaker_id": "7",
        "transcript": "bonjour",
        "file": "1.opus",
        "begin_time": 2,
        "end_time": 2.1,
        "original_path": "https://example.org/recording.mp3",
    }
    for language in ("french", "english"):
        write_tar(
            root / language / "dev-00000.tar.gz",
            [("1.metadata.json", json.dumps(meta).encode()), ("1.flac", audio_bytes())],
        )
    rows = list(mls_sidon.iter_records(root, "test"))
    assert len({r["sample_id"] for r in rows}) == 2
    assert len({r["speaker_id"] for r in rows}) == 2
    for row in rows:
        assert row["source_split"] == "train"
        assert row["audio"]["path"] == "1.flac"
        assert row["segment_start_frame"] is None  # Upstream times are not parent-frame bounds.
        metadata = json.loads(row["metadata_json"])
        assert metadata["original_split"] == "valid"
        assert metadata["upstream"] == meta
    result = preview("mls_sidon", root, "test", tmp_path / "out", 2)
    assert result["fully_decoded_audio"] == 2 and result["audio_bytes_preserved"]


@pytest.mark.parametrize(
    "members,match",
    [
        ([("1.flac", audio_bytes())], "Unpaired"),
        ([("1.flac", audio_bytes()), ("1.flac", audio_bytes())], "Duplicate"),
        ([("1.flac", audio_bytes()), ("1.metadata.json", b'{"id":"2"}')], "does not match"),
    ],
)
def test_mls_rejects_broken_pairing(tmp_path, members, match):
    path = tmp_path / "source.tar.gz"
    write_tar(path, members)
    with pytest.raises(ValueError, match=match):
        list(mls_sidon.archive_pairs(path))


def test_libriheavy_preserves_distinct_text_and_config(tmp_path):
    folder = tmp_path / "default" / "test_clean"
    folder.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "id": "large/speaker/book/clip",
                    "audio": {"bytes": audio_bytes(), "path": "clip.flac"},
                    "speaker_id": "7",
                    "librivox_book_id": "9",
                    "text_original": "Hello!",
                    "text_transcription": "HELLO",
                    "audio_duration": 0.1,
                }
            ]
        ),
        folder / "test.parquet",
    )
    files = libriheavy.input_files(tmp_path, "test_clean")
    row = next(libriheavy.iter_records(tmp_path, "test", files=files))
    assert row["source_config"] == "test_clean" and row["source_split"] == "train"
    assert row["text"] == "Hello!" and row["text_variants"][0]["text"] == "HELLO"
    assert row["recording_id"] is None and row["group_id"] == "libriheavy:book:9"
    with pytest.raises(ValueError, match="Unknown"):
        libriheavy.input_files(tmp_path, "all")
