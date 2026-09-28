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


def corpus(tmp_path):
    import hashlib

    root = tmp_path / "raw"
    folder = root / "game"
    folder.mkdir(parents=True)

    def wav(frames):
        buffer = io.BytesIO()
        sf.write(buffer, np.zeros(frames), 16000, format="WAV")
        return buffer.getvalue()

    good, bad = wav(1600), wav(0)
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
        "condition": "zero_decoded_frames",
        "reason": "fixture exact empty record",
    }
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"dataset_id": "galgame", "release_id": "v0.1", "files": [], "records": [rejected]}
        )
    )
    return root, source, rejected, policy


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
