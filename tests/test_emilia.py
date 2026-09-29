"""Emilia source semantics, strict tar pairing and real publication round trips."""

import io
import json
import tarfile

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import ADAPTERS
from tts_data_pipeline.bulk import bulk_convert
from tts_data_pipeline.convert import convert
from tts_data_pipeline.writer import release_dataset


def mp3():
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(2400), 24000, format="MP3")
    return buffer.getvalue()


def fixture(root, dataset, *, reverse=False, failure=None):
    path = root / "EN/EN-B000000.tar"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = mp3()
    if dataset == "emilia":
        keys = ["EN_B00000_S00000_W000001", "EN_B00000_S00001_W000001"]
        metas = [
            {"id": key, "wav": "/old/machine/" + key + ".mp3", "speaker": key.rsplit("_W", 1)[0]}
            for key in keys
        ]
    else:
        keys = ["EN_a_b-Cde1234_W000001", "EN_z_y-Xwv9876_W000001"]
        metas = [
            {"_id": key, "phone_count": 7, "speaker": key.rsplit("_W", 1)[0] + "_SPEAKER_00"}
            for key in keys
        ]
    for i, meta in enumerate(metas):
        meta.update(
            text=" Hello! " if i == 0 else " ",
            language="en",
            dnsmos=3.2 if i == 0 else None,
            duration=9.0,
            custom={"keep": True},
        )
    if failure == "id":
        metas[0]["id" if dataset == "emilia" else "_id"] = "wrong"
    if failure == "speaker":
        metas[0]["speaker"] = "EN_other_SPEAKER_00"
    if failure == "language":
        metas[0]["language"] = "fr"
    members = [
        (key + ".json", json.dumps(meta).encode()) for key, meta in zip(keys, metas, strict=True)
    ]
    members += [(key + ".mp3", payload) for key in keys]
    if reverse:
        members = list(reversed(members))
    if failure == "missing":
        members = members[:-1]
    if failure == "duplicate":
        members.append(members[-1])
    if failure == "path":
        members[0] = ("../" + members[0][0], members[0][1])
    with tarfile.open(path, "w:") as tar:
        for name, data in members:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return path, payload, metas


@pytest.mark.parametrize("dataset", ["emilia", "emilia_yodas"])
def test_emilia_roundtrip_preserves_audio_scores_scope_and_missing_values(tmp_path, dataset):
    root = tmp_path / "raw"
    path, payload, metas = fixture(root, dataset)
    adapter = ADAPTERS[dataset]
    before = list(adapter.iter_records(root, "fixture"))
    fixture(root, dataset, reverse=True)
    after = list(adapter.iter_records(root, "fixture"))
    assert sorted(before, key=lambda r: r["source_key"]) == sorted(
        after, key=lambda r: r["source_key"]
    )
    result = bulk_convert(dataset, root, tmp_path / "release", workers=1, deep_verify=True)
    assert result["rows"] == 2 and result["validation"]["unique_ids"] == 2
    rows = sorted(
        release_dataset(tmp_path / "release").to_table().to_pylist(), key=lambda r: r["source_key"]
    )
    for row, meta in zip(rows, metas, strict=True):
        assert row["audio"]["bytes"] == payload and row["sample_rate"] == 24000
        assert row["duration_seconds"] != meta["duration"]
        assert row["source_split"] == "train" and row["source_config"] == "EN"
        assert json.loads(row["metadata_json"])["upstream"] == meta
        assert json.loads(row["metadata_json"])["original_split"] is None
        assert not any(k.startswith("ann__") for k in row)
    assert rows[1]["text"] is None
    assert rows[0]["speaker_id"] != rows[1]["speaker_id"]
    if dataset == "emilia_yodas":
        assert rows[0]["recording_id"] == "emilia_yodas:EN_a_b-Cde1234"
        assert rows[0]["group_id"] != rows[1]["group_id"]
    else:
        assert rows[0]["recording_id"] is None
    assert adapter.input_files(root, "EN") == [path]
    with pytest.raises(ValueError):
        adapter.input_files(root, "english")


@pytest.mark.parametrize("dataset", ["emilia", "emilia_yodas"])
@pytest.mark.parametrize("failure", ["missing", "duplicate", "id", "speaker", "language", "path"])
def test_emilia_rejects_bad_pairs_and_conflicting_metadata(tmp_path, dataset, failure):
    fixture(tmp_path, dataset, failure=failure)
    with pytest.raises(ValueError):
        list(ADAPTERS[dataset].iter_records(tmp_path, "fixture"))


@pytest.mark.parametrize("failure", ["truncated_audio", "no_terminator", "trailing_payload"])
def test_emilia_checks_archive_to_end_before_publishing(tmp_path, failure):
    root = tmp_path / "raw"
    path, _, _ = fixture(root, "emilia")
    with tarfile.open(path, "r:") as tar:
        members = list(tar)
    end = members[-1].offset_data + ((members[-1].size + 511) // 512) * 512
    data = path.read_bytes()
    if failure == "truncated_audio":
        data = data[: members[-1].offset_data + 10]
    elif failure == "no_terminator":
        data = data[:end]
    else:
        data = data + b"not zero"
    path.write_bytes(data)
    with pytest.raises((ValueError, tarfile.ReadError)):
        convert("emilia", root, tmp_path / "release")
    assert not (tmp_path / "release").exists()


def test_emilia_corrupt_header_requires_pinned_whole_archive_exclusion(tmp_path):
    from tts_data_pipeline.convert import file_hash

    root = tmp_path / "raw"
    path, _, _ = fixture(root, "emilia_yodas")
    good = path.with_name("EN-B000001.tar")
    good.write_bytes(path.read_bytes())
    with tarfile.open(path, "r:") as archive:
        offset = list(archive)[2].offset
    data = bytearray(path.read_bytes())
    data[offset : offset + 512] = b"X" * 512
    path.write_bytes(data)
    with pytest.raises(ValueError, match="Nonzero content"):
        list(ADAPTERS["emilia_yodas"].iter_records(root, "test", files=[path]))
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "dataset_id": "emilia_yodas",
                "release_id": "v0.1",
                "files": [
                    {
                        "path": "EN/EN-B000000.tar",
                        "bytes": path.stat().st_size,
                        "sha256": file_hash(path),
                        "reason": "corrupt header fixture",
                    }
                ],
            }
        )
    )
    result = bulk_convert(
        "emilia_yodas", root, tmp_path / "release", workers=1, exclusions_path=policy
    )
    assert result["rows"] == 2 and len(result["excluded_source_files"]) == 1
    assert len(result["inputs"]) == 1
    path.write_bytes(bytes(data) + b"changed")
    with pytest.raises(ValueError, match="Excluded source changed"):
        bulk_convert("emilia_yodas", root, tmp_path / "other", workers=1, exclusions_path=policy)
