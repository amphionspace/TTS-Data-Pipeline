import io
import json
import tarfile
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf
import yaml

from tts_data_pipeline.adapters import libriheavy
from tts_data_pipeline.bulk import bulk_convert, finalize, run_batch, source_plan
from tts_data_pipeline.convert import convert


def audio():
    result = io.BytesIO()
    sf.write(result, np.zeros(1600), 16000, format="FLAC")
    return result.getvalue()


def mls_corpus(tmp_path):
    root = tmp_path / "raw"
    (root / "french").mkdir(parents=True)
    paths = []
    for i in range(2):
        path = root / "french" / f"train-{i:05d}.tar.gz"
        paths.append(str(path.relative_to(root)))
        # Deliberate repeated source key in separate shards: retained and reported globally.
        metadata = json.dumps({"id": "1", "speaker_id": str(i), "transcript": "bonjour"}).encode()
        with tarfile.open(path, "w:gz") as archive:
            for name, data in [("1.flac", audio()), ("1.metadata.json", metadata)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    (root / "paths.yaml").write_text(yaml.safe_dump({"french": {"train": paths}}))
    return root


def test_parallel_bulk_tar_publication_and_duplicates(tmp_path):
    root = mls_corpus(tmp_path)
    result = bulk_convert(
        "mls_sidon", root, tmp_path / "out", workers=2, batch_bytes=1, deep_verify=True
    )
    assert result["rows"] == result["validation"]["fully_decoded_audio"] == 2
    assert result["validation"]["unique_ids"] == 2
    assert result["validation"]["repeated_source_keys"] == 1
    assert result["validation"]["repeated_keys_within_config"] == 1
    assert result["source_splits"] == {"train": 2}
    assert result["rejected_rows"] == 0
    assert result["excluded_source_records"] == []
    assert result["checkpoint_code_versions"]
    assert result["finalization"]["phase_seconds"]["total"] > 0
    assert result["code_migration"] is None
    assert (tmp_path / "out" / "manifest.json").exists()
    assert not (tmp_path / "out.incomplete").exists()
    with pytest.raises(FileExistsError):
        bulk_convert("mls_sidon", root, tmp_path / "out")


def test_batch_resume_preserves_verified_output_and_rejects_mutation(tmp_path):
    root = mls_corpus(tmp_path)
    batch = source_plan("mls_sidon", root, 1)[0]
    stage = tmp_path / "stage"
    stage.mkdir()
    job = ("mls_sidon", str(root), str(stage), batch, 1024, False)
    first = run_batch(job)
    shard = next((stage / "samples.lance" / "data").glob("*.lance"))
    original = shard.read_bytes()
    assert not first[2]
    assert run_batch(job)[2]
    assert shard.read_bytes() == original
    shard.write_bytes(original + b"changed")
    with pytest.raises(ValueError, match="Completed output changed"):
        run_batch(job)


def test_mls_missing_archive_rejected_before_work(tmp_path):
    root = mls_corpus(tmp_path)
    next(root.rglob("*.tar.gz")).unlink()
    with pytest.raises(ValueError, match="missing"):
        source_plan("mls_sidon", root, 1024)


def test_global_identity_rejects_duplicate_checkpoint(tmp_path):
    root = mls_corpus(tmp_path)
    batch = source_plan("mls_sidon", root, 1)[0]
    stage = tmp_path / "stage"
    stage.mkdir()
    run_batch(("mls_sidon", str(root), str(stage), batch, 1024, False))
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        finalize(stage, {"batches": [batch, batch], "dataset": "mls_sidon", "deep_verify": False})


@pytest.mark.parametrize("duplicate", [False, True])
def test_final_audit_uses_local_scratch_and_cleans_up_on_success_or_failure(
    tmp_path, monkeypatch, duplicate
):
    import sqlite3

    import tts_data_pipeline.bulk as module

    root = mls_corpus(tmp_path)
    batch = source_plan("mls_sidon", root, 1)[0]
    stage = tmp_path / "shared-output" / "v0.1.incomplete"
    stage.mkdir(parents=True)
    run_batch(("mls_sidon", str(root), str(stage), batch, 1024, False))
    scratch = tmp_path / "local-scratch"
    scratch.mkdir()
    monkeypatch.setattr(module.tempfile, "tempdir", str(scratch))
    paths = []
    connect = sqlite3.connect

    def capture(path, *args, **kwargs):
        paths.append(Path(path))
        assert Path(path).is_relative_to(scratch)
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", capture)
    shards = {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in (stage / "samples.lance/data").glob("*.lance")
    }
    plan = {
        "batches": [batch, batch] if duplicate else [batch],
        "dataset": "mls_sidon",
        "deep_verify": False,
    }
    if duplicate:
        with pytest.raises(sqlite3.IntegrityError):
            finalize(stage, plan)
    else:
        result = finalize(stage, plan)
        assert result["rows"] == result["validation"]["unique_ids"] == 1
        assert result["finalization"]["identity_database_bytes"] > 0
        assert result["finalization"]["phase_seconds"]["total"] > 0
    assert len(paths) == 1 and not paths[0].exists()
    assert not list(scratch.iterdir())
    assert not (module.state_directory(stage) / "identity.sqlite").exists()
    assert all((p.stat().st_size, p.stat().st_mtime_ns) == stats for p, stats in shards.items())


def test_libriheavy_bulk_all_configs_and_checksum(tmp_path):
    root = tmp_path / "raw"
    for config in libriheavy.CONFIGS:
        folder = root / "default" / config
        folder.mkdir(parents=True)
        row = {
            "id": "same-source-key",
            "audio": {"bytes": audio(), "path": "x.flac"},
            "text_original": "Hello",
            "text_transcription": "HELLO",
            "speaker_id": "1",
            "librivox_book_id": "2",
            "audio_duration": 0.1,
        }
        pq.write_table(pa.Table.from_pylist([row]), folder / "x.parquet")
    plan = source_plan("libriheavy", root, 1024**3)
    assert len(plan) == 8
    path = Path(root / plan[0]["files"][0]["path"])
    path.with_suffix(".parquet.sha256").write_text("0" * 64 + "  x.parquet\n")
    with pytest.raises(ValueError, match="checksum sidecar mismatch"):
        convert("libriheavy", root, tmp_path / "bad", config="small")


def test_bulk_resume_after_interrupted_finalization(tmp_path, monkeypatch):
    import tts_data_pipeline.bulk as module

    root = mls_corpus(tmp_path)
    output = tmp_path / "resumable"
    original = module.finalize

    def interrupted(*args):
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(module, "finalize", interrupted)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        bulk_convert("mls_sidon", root, output, workers=1, batch_bytes=1)
    assert not output.exists()
    stage = output.with_name(output.name + ".incomplete")
    stats = {
        str(p.relative_to(stage)): p.stat().st_mtime_ns
        for p in (stage / "samples.lance" / "data").glob("*.lance")
    }
    monkeypatch.setattr(module, "finalize", original)
    result = bulk_convert("mls_sidon", root, output, workers=2, batch_bytes=1, resume=True)
    assert result["rows"] == 2
    assert all((output / p).stat().st_mtime_ns == mtime for p, mtime in stats.items())


def test_cleanup_audit_survives_failed_commit_and_finalization_retry(tmp_path, monkeypatch):
    import tts_data_pipeline.bulk as module

    root = mls_corpus(tmp_path)
    batch = source_plan("mls_sidon", root, 1)[0]
    stage = tmp_path / "v0.1.incomplete"
    stage.mkdir()
    run_batch(("mls_sidon", str(root), str(stage), batch, 1024, False))
    orphan = stage / "samples.lance/data/.tmp-interrupted"
    orphan.write_bytes(b"orphan")
    plan = {"batches": [batch], "dataset": "mls_sidon", "deep_verify": False}
    commit = module.commit_fragments

    def fail(*args):
        raise RuntimeError("commit failed after cleanup")

    monkeypatch.setattr(module, "commit_fragments", fail)
    with pytest.raises(RuntimeError, match="commit failed"):
        finalize(stage, plan)
    assert not orphan.exists()
    monkeypatch.setattr(module, "commit_fragments", commit)
    result = finalize(stage, plan)
    cleanup = result["finalization"]["cleanup"]
    assert cleanup["removed_files"] == 1 and cleanup["removed_bytes"] == 6
    assert [e["action"] for e in cleanup["events"]] == ["planned", "removed"]
    assert all(e["name"] == orphan.name for e in cleanup["events"])


def test_single_lance_table_and_external_control_directories(tmp_path):
    from tts_data_pipeline.bulk import state_directory
    from tts_data_pipeline.writer import release_dataset

    root = mls_corpus(tmp_path)
    output = tmp_path / "unified"
    result = bulk_convert("mls_sidon", root, output, workers=2, batch_bytes=1)
    assert set(p.name for p in output.iterdir()) == {"samples.lance", "manifest.json"}
    assert all(Path(s["path"]).parent == Path("samples.lance/data") for s in result["shards"])
    assert release_dataset(output).count_rows() == result["rows"] == 2
    assert release_dataset(output).describe_indices()
    assert state_directory(output).is_dir()
    for item in result["batch_manifests"]:
        checkpoint = json.loads(
            (state_directory(output) / "checkpoints" / f"{item['task']}.json").read_text()
        )
        assert all((output / s["path"]).is_file() for s in checkpoint["shards"])


def test_retry_uncommitted_flat_shards_does_not_touch_other_tasks(tmp_path):
    from tts_data_pipeline.bulk import checkpoint_path

    root = mls_corpus(tmp_path)
    batches = source_plan("mls_sidon", root, 1)
    stage = tmp_path / "stage"
    stage.mkdir()
    jobs = [("mls_sidon", str(root), str(stage), b, 1024, False) for b in batches]
    for job in jobs:
        run_batch(job)
    first = json.loads(checkpoint_path(stage, batches[0]).read_text())
    second = json.loads(checkpoint_path(stage, batches[1]).read_text())
    neighbor = stage / second["shards"][0]["path"]
    neighbor_stat = neighbor.stat().st_mtime_ns
    checkpoint_path(stage, batches[0]).unlink()  # Interrupted before commit marker.
    (stage / first["shards"][0]["path"]).write_bytes(b"unfinished")
    assert run_batch(jobs[0])[2] is False
    assert neighbor.stat().st_mtime_ns == neighbor_stat
    assert run_batch(jobs[1])[2] is True


@pytest.mark.parametrize(
    "dataset,folder,config",
    [("csemotions", "data", "default"), ("libritts_r", "data/dev.clean", "all")],
)
def test_original_datasets_use_same_full_plan(tmp_path, dataset, folder, config):
    path = tmp_path / folder
    path.mkdir(parents=True)
    (path / "source.parquet").write_bytes(b"planning needs only file stats")
    batches = source_plan(dataset, tmp_path, 1024**3)
    assert len(batches) == 1 and batches[0]["config"] == config


def test_explicit_exclusion_is_recorded_and_source_change_rejected(tmp_path):
    from tts_data_pipeline.convert import file_hash

    root = mls_corpus(tmp_path)
    source = sorted(root.rglob("*.tar.gz"))[0]
    excluded = {
        "path": str(source.relative_to(root)),
        "bytes": source.stat().st_size,
        "sha256": file_hash(source),
        "reason": "fixture approved exclusion",
    }
    policy = tmp_path / "exclusions.json"
    policy.write_text(
        json.dumps({"dataset_id": "mls_sidon", "release_id": "v0.1", "files": [excluded]})
    )
    result = bulk_convert("mls_sidon", root, tmp_path / "out", workers=1, exclusions_path=policy)
    assert result["rows"] == 1
    assert result["excluded_source_files"] == [excluded]
    assert excluded["path"] not in {item["path"] for item in result["inputs"]}
    assert "except" in result["source_selection"]
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="Excluded source changed"):
        source_plan("mls_sidon", root, 1024, [excluded])


def test_resume_refuses_changed_exclusion_policy(tmp_path, monkeypatch):
    import tts_data_pipeline.bulk as module
    from tts_data_pipeline.convert import file_hash

    root = mls_corpus(tmp_path)
    source = sorted(root.rglob("*.tar.gz"))[0]
    policy = tmp_path / "exclusions.json"
    policy.write_text(
        json.dumps(
            {
                "dataset_id": "mls_sidon",
                "release_id": "v0.1",
                "files": [
                    {
                        "path": str(source.relative_to(root)),
                        "bytes": source.stat().st_size,
                        "sha256": file_hash(source),
                        "reason": "fixture approved exclusion",
                    }
                ],
            }
        )
    )

    def interrupted(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(module, "finalize", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        bulk_convert("mls_sidon", root, tmp_path / "out", workers=1, exclusions_path=policy)
    with pytest.raises(ValueError, match="same source exclusions"):
        bulk_convert("mls_sidon", root, tmp_path / "out", workers=1, resume=True)
