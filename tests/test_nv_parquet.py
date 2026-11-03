"""NonverbalTTS, DisfluencySpeech and NonVerbalSpeech-38K adapters: text, splits, publication."""

import io
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import ADAPTERS
from tts_data_pipeline.bulk import bulk_convert
from tts_data_pipeline.writer import release_dataset


def wav(seed):
    buffer = io.BytesIO()
    sf.write(buffer, np.full(1600, seed / 100.0), 16000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def nonverbaltts(root):
    for i, split in enumerate(("train", "dev", "test", "other")):
        write(
            root / "default" / split / "0000.parquet",
            [
                {
                    "index": f"id{i}/clip/0000{i}",
                    "audio": {"bytes": wav(i + 1), "path": f"id{i}_clip_0000{i}.wav"},
                    "Emotion": "happy" if i else None,
                    "Initial text": "So, Mom, 🌬️ how are you?",
                    "Annotator response 1": "So, Mom, how are you?",
                    "Annotator response 2": None,
                    "Annotator response 3": "So, Mom, 🌬️ how are you?",
                    "Result": "So, Mom, 🌬️ how are you?" if i != 3 else " ",
                    "dnsmos": 3.5,
                    "duration": 0.1,
                    "speaker_id": f"id{i}",
                    "data_name": "VoxCeleb",
                    "gender": "f",
                }
            ],
        )


def disfluency(root):
    for i, split in enumerate(("train", "validation", "test")):
        write(
            root / "data" / f"{split}-00000-of-00001.parquet",
            [
                {
                    "audio": {"bytes": wav(i + 1), "path": f"{100 + i}.wav"},
                    "transcript_annotated": "Yeah. <laughter> {D Well, } rabbits.",
                    "transcript_a": "Yeah. Well, rabbits.",
                    "transcript_b": "Yeah. rabbits.",
                    "transcript_c": "" if i else "Yeah. rabbits.",
                }
            ],
        )


def published(dataset, root, tmp_path):
    result = bulk_convert(dataset, root, tmp_path / "release", workers=1, deep_verify=True)
    rows = release_dataset(tmp_path / "release").to_table().to_pylist()
    assert result["rows"] == len(rows) == result["validation"]["unique_ids"]
    return {r["source_key"]: r for r in rows}


def test_nonverbaltts_keeps_fused_text_variants_and_splits(tmp_path):
    root = tmp_path / "raw"
    nonverbaltts(root)
    rows = published("nonverbaltts", root, tmp_path)
    first = rows["id0/clip/00000"]
    assert first["text"] == "So, Mom, 🌬️ how are you?"
    assert first["text_kind"] == "source_transcript_paralinguistic"
    assert first["speaker_id"] == "nonverbaltts:id0" and first["language"] == "en"
    assert first["text_variants"] == [
        {"kind": "source_asr_paralinguistic", "text": "So, Mom, 🌬️ how are you?", "language": "en"}
    ]
    meta = json.loads(first["metadata_json"])
    assert meta["annotator_responses"][1] is None and meta["original_split_group"] == "train"
    assert json.loads(rows["id2/clip/00002"]["metadata_json"])["original_split_group"] == "test"
    blank = rows["id3/clip/00003"]
    assert blank["text"] is None and blank["text_kind"] is None


def test_disfluencyspeech_keeps_annotated_text_and_reduced_variants(tmp_path):
    root = tmp_path / "raw"
    disfluency(root)
    rows = published("disfluencyspeech", root, tmp_path)
    assert set(rows) == {"100", "101", "102"}
    first = rows["100"]
    assert first["text"] == "Yeah. <laughter> {D Well, } rabbits."
    assert first["text_kind"] == "source_transcript_annotated"
    assert [v["kind"] for v in first["text_variants"]] == [
        "source_transcript_no_events",
        "source_transcript_no_fillers",
        "source_transcript_no_false_starts",
    ]
    assert len(rows["101"]["text_variants"]) == 2
    assert json.loads(rows["102"]["metadata_json"])["original_split_group"] == "test"
    assert first["speaker_id"] == "disfluencyspeech:speaker"


def test_disfluencyspeech_rejects_missing_split(tmp_path):
    disfluency(tmp_path)
    (tmp_path / "data" / "test-00000-of-00001.parquet").unlink()
    with pytest.raises(ValueError):
        ADAPTERS["disfluencyspeech"].input_files(tmp_path)


def test_nonverbaltts_rejects_unknown_split(tmp_path):
    nonverbaltts(tmp_path)
    write(tmp_path / "default" / "extra" / "0000.parquet", [])
    with pytest.raises(ValueError):
        ADAPTERS["nonverbaltts"].input_files(tmp_path)


def nvs(root, count=2, language="ZH"):
    for i in range(count):
        write(
            root / "data" / f"train-{i:05d}-of-{count:05d}.parquet",
            [
                {
                    "duration": 0.1,
                    "non_verbal_region": [0.0, 0.05],
                    "v1_caption": " [laughing] 你好<B>呀</B> ",
                    "source": "Cartoon",
                    "language": language if i else "EN",
                    "label": "laughing",
                    "audio": {"bytes": wav(i + 1), "path": None},
                    "caption": "[laughing]你好呀。",
                }
            ],
        )


def test_nonverbalspeech38k_keeps_caption_language_and_region(tmp_path):
    root = tmp_path / "raw"
    nvs(root)
    rows = published("nonverbalspeech38k", root, tmp_path)
    zh = rows["data/train-00001-of-00002.parquet#row=0"]
    assert zh["text"] == "[laughing]你好呀。" and zh["text_kind"] == "source_asr_paralinguistic"
    assert zh["language"] == "zh" and zh["speaker_id"] is None
    assert zh["text_variants"] == [
        {
            "kind": "source_asr_paralinguistic_v1",
            "text": "[laughing] 你好<B>呀</B>",
            "language": "zh",
        }
    ]
    meta = json.loads(zh["metadata_json"])
    assert meta["nonverbal_label"] == "laughing" and meta["nonverbal_region"] == [0.0, 0.05]
    assert rows["data/train-00000-of-00002.parquet#row=0"]["language"] == "en"


@pytest.mark.parametrize("failure", ["missing_shard", "language"])
def test_nonverbalspeech38k_rejects_bad_input(tmp_path, failure):
    nvs(tmp_path, language="FR" if failure == "language" else "ZH")
    if failure == "missing_shard":
        (tmp_path / "data" / "train-00001-of-00002.parquet").unlink()
    with pytest.raises(ValueError):
        adapter = ADAPTERS["nonverbalspeech38k"]
        list(adapter.iter_records(tmp_path, "fixture", files=adapter.input_files(tmp_path)))
