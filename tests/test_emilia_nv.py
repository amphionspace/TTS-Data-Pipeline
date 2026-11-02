"""Emilia-NV webdataset triples: tagged text, category agreement and publication."""

import io
import json
import tarfile

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import ADAPTERS
from tts_data_pipeline.bulk import bulk_convert
from tts_data_pipeline.writer import release_dataset


def audio(fmt):
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(2400), 24000, format=fmt)
    return buffer.getvalue()


def fixture(root, *, reverse=False, failure=None):
    path = root / "webdataset-000003.tar"
    root.mkdir(parents=True, exist_ok=True)
    texts = ["你好[Laughter]，再见[Breathing]。", "[Uhm]嗯。"]
    metas = [{"category": "[Laughter],[Breathing]"}, {"category": "[Uhm]"}]
    payloads = [audio("MP3"), audio("WAV")]
    if failure == "category":
        metas[1] = {"category": "[Sigh]"}
    if failure == "unknown_tag":
        texts[1], metas[1] = "[Foo]嗯。", {"category": "[Foo]"}
    if failure == "extra_field":
        metas[1]["speaker"] = "x"
    keys = ["000003_000000", "000003_000001" if failure != "prefix" else "000004_000001"]
    members = []
    for key, text, meta, payload in zip(keys, texts, metas, payloads, strict=True):
        members += [
            (key + ".wav", payload),
            (key + ".txt", text.encode()),
            (key + ".json", json.dumps(meta).encode()),
        ]
    if reverse:
        members = list(reversed(members))
    if failure == "missing":
        members = members[:-1]
    if failure == "duplicate":
        members.append(members[-2])
    if failure == "path":
        members[0] = ("../" + members[0][0], members[0][1])
    with tarfile.open(path, "w:") as tar:
        for name, data in members:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return path, payloads, texts, metas


def test_emilia_nv_roundtrip_keeps_tags_bytes_and_category(tmp_path):
    root = tmp_path / "raw"
    path, payloads, texts, metas = fixture(root)
    adapter = ADAPTERS["emilia_nv"]
    before = list(adapter.iter_records(root, "fixture"))
    fixture(root, reverse=True)
    after = list(adapter.iter_records(root, "fixture"))
    assert sorted(before, key=lambda r: r["source_key"]) == sorted(
        after, key=lambda r: r["source_key"]
    )
    result = bulk_convert("emilia_nv", root, tmp_path / "release", workers=1, deep_verify=True)
    assert result["rows"] == 2 and result["validation"]["unique_ids"] == 2
    rows = sorted(
        release_dataset(tmp_path / "release").to_table().to_pylist(), key=lambda r: r["source_key"]
    )
    for row, payload, text, meta in zip(rows, payloads, texts, metas, strict=True):
        assert row["audio"]["bytes"] == payload and row["audio"]["path"].endswith(".wav")
        assert row["text"] == text and row["text_kind"] == "source_asr_paralinguistic"
        assert row["language"] == "zh" and row["speaker_id"] is None
        assert json.loads(row["metadata_json"])["upstream"] == meta
    assert json.loads(rows[0]["metadata_json"])["paralinguistic_tags"] == [
        "[Breathing]",
        "[Laughter]",
    ]
    assert json.loads(rows[0]["source_locator_json"])["path"] == "Emilia-NV/webdataset-000003.tar"
    assert adapter.input_files(root) == [path]


@pytest.mark.parametrize(
    "failure",
    ["missing", "duplicate", "path", "category", "unknown_tag", "extra_field", "prefix"],
)
def test_emilia_nv_rejects_bad_triples(tmp_path, failure):
    fixture(tmp_path, failure=failure)
    with pytest.raises(ValueError):
        list(ADAPTERS["emilia_nv"].iter_records(tmp_path, "fixture"))


def test_emilia_nv_requires_tar_terminator(tmp_path):
    path, _, _, _ = fixture(tmp_path)
    with tarfile.open(path, "r:") as tar:
        last = list(tar)[-1]
    path.write_bytes(path.read_bytes()[: last.offset_data + ((last.size + 511) // 512) * 512])
    with pytest.raises(ValueError, match="terminator"):
        list(ADAPTERS["emilia_nv"].iter_records(tmp_path, "fixture"))
