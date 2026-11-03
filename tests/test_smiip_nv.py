"""SMIIP-NV zips: GBK names, point/span annotations, intervals and the repacked split."""

import io
import json
import zipfile

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import ADAPTERS
from tts_data_pipeline.bulk import bulk_convert
from tts_data_pipeline.writer import release_dataset


def wav(seed):
    buffer = io.BytesIO()
    sf.write(buffer, np.full(1600, seed / 100.0), 16000, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def gbk_zip(path, members):
    """Raw GBK names without the UTF-8 flag, like the upstream archive.

    zipfile flags any non-ASCII name as UTF-8, so the emotion folder is written as a same-length
    ASCII placeholder and swapped for its GBK bytes afterwards (names are outside the CRC).
    """
    folder = "伤心".encode("gbk")
    placeholder = b"Q" * len(folder)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name.replace("伤心", placeholder.decode()), data)
    path.write_bytes(buffer.getvalue().replace(placeholder, folder))


def fixture(root, *, failure=None):
    root.mkdir(parents=True, exist_ok=True)
    clips = {"MIC-0001_0001": wav(1), "MIC-0001_0002": wav(2), "MIC-0001_0003": wav(3)}
    lines = [
        "MIC-0001_0001.wav\t好的<crying>，再见。\t[0.1, 0.2]",
        "MIC-0001_0002.wav\t  <crying/>别走</crying>。\t[0.0,0.5]",
        "MIC-0001_0001.wav\t好的，<crying/>再见</crying>。\t[0.05, 0.3]",
    ]
    if failure == "interval":
        lines[0] = "MIC-0001_0001.wav\t好的<crying>。\t[0.3, 0.2]"
    if failure == "two_points":
        lines.append("MIC-0001_0001.wav\t好<crying>的。\t[0.1, 0.2]")
    main = [(f"SMIIP_NV_Up/MIC-0001/伤心/{key}.wav", data) for key, data in clips.items()]
    main.append(("SMIIP_NV_Up/MIC-0001/伤心/annotation.txt", "\n".join(lines).encode()))
    splits = {
        "MIC-0001_0001": "spk_train",
        "MIC-0001_0002": "spk_test",
        "MIC-0001_0003": "spk_train",
    }
    repack = []
    for key, data in clips.items():
        if failure == "changed_audio" and key == "MIC-0001_0003":
            data = wav(9)
        repack += [
            (f"SMIIP_NV_SPK_finetune/{splits[key]}/{key}.wav", data),
            (f"SMIIP_NV_SPK_finetune/{splits[key]}/{key}.txt", b"x"),
        ]
    if failure == "missing_clip":
        repack = repack[:-2]
    gbk_zip(root / "SMIIP_NV.zip", main)
    gbk_zip(root / "SMIIP_NV_SPK_finetune.zip", repack)
    return clips


def test_smiip_nv_publishes_point_text_span_variants_and_split(tmp_path):
    root = tmp_path / "raw"
    clips = fixture(root)
    result = bulk_convert("smiip_nv", root, tmp_path / "release", workers=1, deep_verify=True)
    assert result["rows"] == 3
    rows = {
        r["source_key"]: r for r in release_dataset(tmp_path / "release").to_table().to_pylist()
    }
    first = rows["MIC-0001_0001"]
    assert first["audio"]["bytes"] == clips["MIC-0001_0001"]
    assert first["text"] == "好的<crying>，再见。" and first["language"] == "zh"
    assert first["text_kind"] == "source_transcript_paralinguistic"
    assert first["speaker_id"] == "smiip_nv:MIC-0001"
    assert first["text_variants"] == [
        {
            "kind": "source_transcript_paralinguistic_span",
            "text": "好的，<crying/>再见</crying>。",
            "language": "zh",
        }
    ]
    meta = json.loads(first["metadata_json"])
    assert meta["emotion"] == "sad" and meta["text_style"] == "point"
    assert [a["intervals"] for a in meta["annotations"]] == [[[0.1, 0.2]], [[0.05, 0.3]]]
    assert json.loads(first["source_locator_json"])["member"].endswith("伤心/MIC-0001_0001.wav")
    span_only = rows["MIC-0001_0002"]
    assert span_only["text"] == "<crying/>别走</crying>。" and span_only["text_variants"] == []
    assert json.loads(span_only["metadata_json"])["original_split_group"] == "test"
    missing = rows["MIC-0001_0003"]
    assert missing["text"] is None and json.loads(missing["metadata_json"])["missing_annotation"]


@pytest.mark.parametrize("failure", ["interval", "two_points", "changed_audio", "missing_clip"])
def test_smiip_nv_rejects_inconsistent_archives(tmp_path, failure):
    fixture(tmp_path, failure=failure)
    with pytest.raises(ValueError):
        list(ADAPTERS["smiip_nv"].iter_records(tmp_path, "fixture"))
