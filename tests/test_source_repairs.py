"""Regressions for worker-thread migration and narrowly pinned source rejections."""

import io
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.adapters._archives import pairs
from tts_data_pipeline.bulk import bulk_convert, source_plan, state_directory
from tts_data_pipeline.convert import convert, file_hash
from tts_data_pipeline.writer import release_dataset


def test_archive_cache_can_migrate_threads_without_losing_spooled_pairs():
    with pairs() as store, ThreadPoolExecutor(max_workers=1) as worker:
        store.put("a", data=b"first audio")
        assert worker.submit(store.put, "a", metadata={"text": "a"}).result() == (
            b"first audio",
            {"text": "a"},
        )
        worker.submit(store.put, "b", metadata={"text": "b"}).result()
        assert store.put("b", data=b"second audio") == (b"second audio", {"text": "b"})
        worker.submit(store.finish).result()
        assert store.count == 2


def corpus(tmp_path, bad_audio=None, condition="zero_decoded_frames"):
    import hashlib

    root = tmp_path / "raw"
    folder = root / "game"
    folder.mkdir(parents=True)

    def wav(frames):
        buffer = io.BytesIO()
        sf.write(buffer, np.zeros(frames), 16000, format="WAV")
        return buffer.getvalue()

    good, bad = wav(1600), wav(0) if bad_audio is None else bad_audio
    rows = [
        {"audio": {"bytes": data, "path": f"{i}.wav"}, "audio_ID": str(i), "text": "hello"}
        for i, data in enumerate([good, bad, good])
    ]
    source = folder / "part.parquet"
    pq.write_table(pa.Table.from_pylist(rows), source)
    rejected = {
        "path": "game/part.parquet",
        "row": 1,
        "bytes": source.stat().st_size,
        "sha256": file_hash(source),
        "audio_bytes": len(bad),
        "audio_sha256": hashlib.sha256(bad).hexdigest(),
        "audio_ID": "1",
        "condition": condition,
        "reason": "fixture exact rejected record",
    }
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"dataset_id": "galgame", "release_id": "v0.1", "files": [], "records": [rejected]}
        )
    )
    return root, source, rejected, policy


def malformed_opus():
    """Valid Opus packets and OGG CRC, but EOS granule exceeds available samples."""
    import struct

    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(4800), 48000, format="OGG", subtype="OPUS")
    data = bytearray(buffer.getvalue())
    offset = 0
    while offset < len(data):
        segments = data[offset + 26]
        length = 27 + segments + sum(data[offset + 27 : offset + 27 + segments])
        if data[offset + 5] & 4:
            struct.pack_into("<q", data, offset + 6, 10_000_000)
            data[offset + 22 : offset + 26] = b"\x00" * 4
            crc = 0
            for byte in data[offset : offset + length]:
                crc ^= byte << 24
                for _ in range(8):
                    crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
            struct.pack_into("<I", data, offset + 22, crc)
        offset += length
    return bytes(data)


def test_exact_malformed_record_rejection_preserves_neighbors_and_accounts_for_rows(tmp_path):
    root, source, rejected, policy = corpus(tmp_path, malformed_opus(), "sndfile_malformed")
    with pytest.raises(sf.LibsndfileError) as error:
        convert("galgame", root, tmp_path / "unfiltered")
    assert error.value.code == 3
    result = bulk_convert("galgame", root, tmp_path / "filtered", workers=1, exclusions_path=policy)
    assert result["rows"] == 2 and result["rejected_rows"] == 1
    assert result["excluded_source_records"] == [rejected]
    assert result["inputs"][0]["rows"] == 3
    rows = release_dataset(tmp_path / "filtered").to_table().to_pylist()
    assert {r["source_key"] for r in rows} == {"game/part.parquet#row=0", "game/part.parquet#row=2"}
    assert file_hash(source) == rejected["sha256"]
    with pytest.raises(ValueError, match="Excluded record changed"):
        convert("galgame", root, tmp_path / "wrong", record_exclusions=[{**rejected, "row": 0}])


def test_malformed_policy_does_not_hide_other_errors_or_readable_audio():
    import hashlib

    from tts_data_pipeline.source_exclusions import verify_excluded_row

    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(100), 16000, format="WAV")
    for data, message in [
        (b"not audio", "different decoder error"),
        (buffer.getvalue(), "no longer rejected as sndfile_malformed"),
    ]:
        row = {"audio_ID": "fixture", "audio": {"bytes": data}}
        item = {
            "audio_ID": "fixture",
            "audio_bytes": len(data),
            "audio_sha256": hashlib.sha256(data).hexdigest(),
            "condition": "sndfile_malformed",
        }
        with pytest.raises(ValueError, match=message):
            verify_excluded_row(row, item)


def test_exact_empty_record_exclusion_accounts_for_footer_without_renumbering(tmp_path):
    root, source, rejected, policy = corpus(tmp_path)
    with pytest.raises(ValueError, match="Empty audio"):
        convert("galgame", root, tmp_path / "unfiltered")
    result = bulk_convert("galgame", root, tmp_path / "filtered", workers=1, exclusions_path=policy)
    assert result["rows"] == 2 and result["rejected_rows"] == 1
    assert result["excluded_source_records"] == [rejected]
    assert result["inputs"][0]["rows"] == 3
    rows = release_dataset(tmp_path / "filtered").to_table().to_pylist()
    assert {r["source_key"] for r in rows} == {"game/part.parquet#row=0", "game/part.parquet#row=2"}
    before = file_hash(source)
    assert before == rejected["sha256"]
    wrong = {**rejected, "row": 0}
    with pytest.raises(ValueError, match="Excluded record changed"):
        convert("galgame", root, tmp_path / "wrong-row", record_exclusions=[wrong])
    absent = {**rejected, "row": 999}
    # An exclusion cannot silently suppress another unexpected invalid row.
    with pytest.raises(ValueError, match="Empty audio"):
        convert("galgame", root, tmp_path / "absent", record_exclusions=[absent])
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="Excluded source changed"):
        source_plan("galgame", root, 1024**3, record_exclusions=[rejected])


def test_resume_refuses_changed_record_policy(tmp_path, monkeypatch):
    import tts_data_pipeline.bulk as module

    root, _, _, policy = corpus(tmp_path)

    def stop(*args):
        raise RuntimeError("stop before publication")

    monkeypatch.setattr(module, "finalize", stop)
    with pytest.raises(RuntimeError, match="stop before"):
        bulk_convert("galgame", root, tmp_path / "output", workers=1, exclusions_path=policy)
    with pytest.raises(ValueError, match="same record exclusions"):
        bulk_convert("galgame", root, tmp_path / "output", workers=1, resume=True)


def test_explicit_checkpoint_code_migration_preserves_actual_code_evidence(tmp_path, monkeypatch):
    from test_bulk import mls_corpus

    import tts_data_pipeline.bulk as module

    root = mls_corpus(tmp_path)
    output = tmp_path / "out"
    original = module.finalize

    def stop(*args):
        raise RuntimeError("stop")

    monkeypatch.setattr(module, "finalize", stop)
    with pytest.raises(RuntimeError, match="stop"):
        bulk_convert("mls_sidon", root, output, workers=1, batch_bytes=1)
    stage = output.with_name("out.incomplete")
    state = state_directory(stage)
    plan = json.loads((state / "plan.json").read_text())
    checkpoint = sorted((state / "checkpoints").glob("*.json"))[0]
    manifest = json.loads(checkpoint.read_text())
    older = {"fixture_old_code.py": "a" * 64}
    manifest["code_sha256"] = older
    checkpoint.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="dependency/code versions differ"):
        original(stage, plan)
    plan["compatible_checkpoint_code_sha256"] = [plan["code_sha256"]]
    with pytest.raises(ValueError, match="Unapproved checkpoint"):
        original(stage, plan)
    plan["compatible_checkpoint_code_sha256"].append(older)
    plan["code_migration"] = {"reason": "fixture reviewed semantic compatibility"}
    result = original(stage, plan)
    assert len(result["checkpoint_code_versions"]) == 2
    assert older in result["checkpoint_code_versions"].values()
    assert result["code_sha256"] == plan["code_sha256"]
    assert json.loads(checkpoint.read_text())["code_sha256"] == older


def test_native_lance_consumes_multiple_archive_batches(tmp_path):
    from test_adapters import archive

    root = tmp_path / "raw"
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(5 * 1024**2, dtype=np.int16), 16000, format="WAV", subtype="PCM_16")
    data = buffer.getvalue()
    members = [("LJ/metadata.csv", b"LJ001-1|a|a\nLJ001-2|b|b\nLJ001-3|c|c\nLJ001-4|d|d\n")]
    members.extend((f"LJ/wavs/LJ001-{i}.wav", data) for i in range(1, 5))
    archive(root / "source.tar.bz2", members)
    result = convert("ljspeech", root, tmp_path / "out")
    assert result["rows"] == 4 and result["audio_bytes"] == 4 * len(data)


def test_unrecognised_record_is_excluded_without_renumbering(tmp_path):
    root, source, rejected, _ = corpus(tmp_path, b"1" + b"\x00" * 23, "sndfile_unrecognised")
    result = convert("galgame", root, tmp_path / "out", record_exclusions=[rejected])
    assert result["rows"] == 2 and result["rejected_rows"] == 1
    assert result["inputs"][0]["rows"] == 3
    rows = release_dataset(tmp_path / "out").to_table().to_pylist()
    assert {r["source_key"] for r in rows} == {"game/part.parquet#row=0", "game/part.parquet#row=2"}
    assert file_hash(source) == rejected["sha256"]


def test_libriheavy_exact_rejection_preserves_ids_and_text(tmp_path):
    import hashlib

    from test_mls_libriheavy import audio_bytes

    from tts_data_pipeline.source_exclusions import validate_record_exclusions

    root = tmp_path / "raw"
    source = root / "default/large/part.parquet"
    source.parent.mkdir(parents=True)
    bad = malformed_opus()
    rows = [
        {
            "id": f"clip-{i}",
            "audio": {"bytes": data, "path": f"{i}.opus"},
            "text_original": "Book text",
            "text_transcription": "ASR text",
            "speaker_id": "7",
        }
        for i, data in enumerate([audio_bytes(), bad, audio_bytes()])
    ]
    pq.write_table(pa.Table.from_pylist(rows), source)
    rejected = {
        "path": "default/large/part.parquet",
        "row": 1,
        "id": "clip-1",
        "bytes": source.stat().st_size,
        "sha256": file_hash(source),
        "audio_bytes": len(bad),
        "audio_sha256": hashlib.sha256(bad).hexdigest(),
        "condition": "sndfile_malformed",
        "reason": "reviewed fixture",
    }
    with pytest.raises(sf.LibsndfileError):
        convert("libriheavy", root, tmp_path / "bad", config="large")
    result = convert(
        "libriheavy", root, tmp_path / "out", config="large", record_exclusions=[rejected]
    )
    assert result["rows"] == 2 and result["rejected_rows"] == 1
    output = release_dataset(tmp_path / "out").to_table().to_pylist()
    assert [r["source_key"] for r in output] == ["clip-0", "clip-2"]
    assert all(
        r["text"] == "Book text" and r["text_variants"][0]["text"] == "ASR text" for r in output
    )
    assert file_hash(source) == rejected["sha256"]
    with pytest.raises(ValueError, match="original ID field"):
        validate_record_exclusions("libriheavy", [{**rejected, "audio_ID": "clip-1"}])
    with pytest.raises(ValueError, match="Excluded record changed"):
        convert(
            "libriheavy",
            root,
            tmp_path / "wrong",
            config="large",
            record_exclusions=[{**rejected, "id": "wrong"}],
        )


def wenet_missing_transcript(tmp_path):
    import hashlib

    from test_adapters import archive, audio

    root = tmp_path / "raw"
    prefix = "WenetSpeech4TTS_Basic_0"
    path = root / "Basic" / (prefix + ".tar.gz")
    data = audio()
    keys = [f"X1_S0000{i}" for i in range(3)]
    members = [(f"{prefix}/wavs/{key}.wav", data) for key in keys]
    members.extend(
        (f"{prefix}/txts/{key}.txt", f"{key}\thello\n".encode()) for key in (keys[0], keys[2])
    )
    archive(path, members)
    (root / "filelists").mkdir()
    (root / "DNSMOS_P808Scores").mkdir()
    (root / "filelists/Basic_filelist.lst").write_text(
        "".join(
            f"{key}\t../Basic/{prefix}/wavs/{key}.wav\t../Basic/{prefix}/txts/{key}.txt\n"
            for key in keys
        )
    )
    (root / "DNSMOS_P808Scores/Basic_DNSMOS.lst").write_text(
        "".join(f"{key}\t3.6\n" for key in keys)
    )
    item = {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": file_hash(path),
        "source_key": keys[1],
        "audio_member": f"{prefix}/wavs/{keys[1]}.wav",
        "missing_member": f"{prefix}/txts/{keys[1]}.txt",
        "audio_bytes": len(data),
        "audio_sha256": hashlib.sha256(data).hexdigest(),
        "condition": "missing_transcript",
        "reason": "reviewed fixture: one declared transcript absent",
    }
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"dataset_id": "wenetspeech4tts", "release_id": "v0.1", "files": [], "records": [item]}
        )
    )
    return root, path, members, keys, item, policy


def test_wenet_missing_transcript_exact_exclusion_accounts_for_declared_records(tmp_path):
    from tts_data_pipeline.adapters import wenetspeech4tts

    root, path, members, keys, item, policy = wenet_missing_transcript(tmp_path)
    with pytest.raises(ValueError, match="Unpaired"):
        convert("wenetspeech4tts", root, tmp_path / "unfiltered")
    result = bulk_convert(
        "wenetspeech4tts", root, tmp_path / "out", workers=1, exclusions_path=policy
    )
    assert result["rows"] == 2 and result["rejected_rows"] == 1
    assert result["excluded_source_records"] == [item]
    restored = release_dataset(tmp_path / "out").to_table().to_pylist()
    assert [r["source_key"] for r in restored] == [keys[0], keys[2]]
    assert all(json.loads(r["metadata_json"])["upstream_dnsmos_p808"] == 3.6 for r in restored)
    assert file_hash(path) == item["sha256"]
    with pytest.raises(ValueError, match="Excluded record changed"):
        list(
            wenetspeech4tts.iter_records(
                root, "fixture", excluded_records=[{**item, "audio_sha256": "0" * 64}]
            )
        )
    with pytest.raises(ValueError, match="source changed"):
        convert(
            "wenetspeech4tts",
            root,
            tmp_path / "wrong-source",
            record_exclusions=[{**item, "sha256": "0" * 64}],
        )


@pytest.mark.parametrize(
    "failure", ["restored_text", "missing_audio", "another_missing_text", "duplicate_audio"]
)
def test_wenet_rejection_does_not_hide_other_archive_changes(tmp_path, failure):
    from test_adapters import archive

    from tts_data_pipeline.adapters import wenetspeech4tts

    root, path, members, keys, item, policy = wenet_missing_transcript(tmp_path)
    if failure == "restored_text":
        members.append((item["missing_member"], f"{keys[1]}\trestored".encode()))
    elif failure == "missing_audio":
        members = [m for m in members if m[0] != item["audio_member"]]
    elif failure == "another_missing_text":
        members = [m for m in members if not m[0].endswith(keys[0] + ".txt")]
    else:
        members.append(next(m for m in members if m[0] == item["audio_member"]))
    archive(path, members)
    with pytest.raises(ValueError):
        list(wenetspeech4tts.iter_records(root, "fixture", excluded_records=[item]))
