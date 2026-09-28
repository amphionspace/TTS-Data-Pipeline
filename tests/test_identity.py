"""Identity must survive scheduling and relocation, but track source-file mutations."""

import io
import json
import shutil
import tarfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.bulk import bulk_convert, checkpoint_path, run_batch, source_plan
from tts_data_pipeline.convert import convert
from tts_data_pipeline.schema import IDENTITY_SCHEME


def write_source(root, dataset, index, text="hello"):
    audio = io.BytesIO()
    sf.write(audio, np.zeros(1600), 16000, format="FLAC")
    row = {
        "audio": {"bytes": audio.getvalue(), "path": "clip.flac"},
        "id": "same-upstream-id",
        "speaker_id": "7",
        "text_original": text,
        "text_normalized": text,
        "text_transcription": text,
        "chapter_id": "8",
        "librivox_book_id": "9",
        "path": "clip.flac",
        "audio_duration": 0.1,
        "text": text,
        "speaker": "7",
        "emotion": "neutral",
    }
    if dataset == "mls_sidon":
        path = root / "french" / f"train-{index:05d}.tar.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = json.dumps({"id": row["id"], "speaker_id": "7", "transcript": text}).encode()
        with tarfile.open(path, "w:gz") as archive:
            for name, value in [
                ("same-upstream-id.flac", audio.getvalue()),
                ("same-upstream-id.metadata.json", metadata),
            ]:
                member = tarfile.TarInfo(name)
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
    else:
        folder = {
            "csemotions": "data",
            "libritts_r": "data/dev.clean",
            "libriheavy": "default/small",
        }[dataset]
        path = root / folder / f"part-{index}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist([row]), path)
    return path


def records(output):
    from tts_data_pipeline.writer import release_dataset

    rows = release_dataset(output).to_table().to_pylist()
    return {r["source_locator_json"]: r for r in rows}


@pytest.mark.parametrize("dataset", ["csemotions", "libritts_r", "libriheavy", "mls_sidon"])
def test_identity_survives_regrouping_selection_sharding_and_root_move(tmp_path, dataset):
    root = tmp_path / "raw"
    files = [write_source(root, dataset, i) for i in range(2)]
    single = tmp_path / "single"
    joined = tmp_path / "joined"
    convert(dataset, root, single, files=files[:1], shard_bytes=1)
    result = convert(dataset, root, joined, files=list(reversed(files)), shard_bytes=1024**2)
    first, all_rows = records(single), records(joined)
    assert len(all_rows) == 2  # Same source key in distinct files stays distinct in every batch.
    assert len({r["sample_id"] for r in all_rows.values()}) == 2
    assert all(all_rows[key] == value for key, value in first.items())
    assert result["identity_scheme"] == IDENTITY_SCHEME
    assert len({r["source_snapshot"] for r in all_rows.values()}) == 2
    moved = tmp_path / "moved-raw"
    shutil.copytree(root, moved)
    relocated = tmp_path / "relocated"
    convert(
        dataset, moved, relocated, files=[moved / f.relative_to(root) for f in files], shard_bytes=1
    )
    assert records(relocated) == all_rows
    # Same audio bytes, changed source metadata: only that source unit gets new identities.
    write_source(root, dataset, 0, text="corrected")
    changed = tmp_path / "changed"
    convert(dataset, root, changed, files=files)
    after = records(changed)
    for key, row in all_rows.items():
        assert after[key]["audio_sha256"] == row["audio_sha256"]
        if key in first:
            assert after[key]["sample_id"] != row["sample_id"]
        else:
            assert after[key] == row


def mls_root(tmp_path):
    import yaml

    root = tmp_path / "raw"
    paths = [str(write_source(root, "mls_sidon", i).relative_to(root)) for i in range(2)]
    (root / "paths.yaml").write_text(yaml.safe_dump({"french": {"train": paths}}))
    return root


def test_bulk_worker_and_batch_size_do_not_change_record_identity(tmp_path):
    root = mls_root(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    bulk_convert("mls_sidon", root, a, workers=1, batch_bytes=1, shard_bytes=1)
    bulk_convert("mls_sidon", root, b, workers=2, batch_bytes=1024**2, shard_bytes=1024**2)
    assert records(a) == records(b)


def test_legacy_checkpoint_rejected_without_touching_audio(tmp_path):
    root = mls_root(tmp_path)
    batch = source_plan("mls_sidon", root, 1)[0]
    stage = tmp_path / "stage"
    stage.mkdir()
    job = ("mls_sidon", str(root), str(stage), batch, 1024, False)
    run_batch(job)
    path = checkpoint_path(stage, batch)
    old = json.loads(path.read_text())
    del old["identity_scheme"]
    path.write_text(json.dumps(old))
    before = {p: p.read_bytes() for p in (stage / "samples.lance" / "data").glob("*.lance")}
    with pytest.raises(ValueError, match="incompatible identity scheme"):
        run_batch(job)
    assert all(p.read_bytes() == data for p, data in before.items())


def test_legacy_plan_rejected_before_resume_writes(tmp_path):
    from tts_data_pipeline.bulk import state_directory

    root = mls_root(tmp_path)
    output = tmp_path / "old-release"
    stage = output.with_name(output.name + ".incomplete")
    stage.mkdir()
    state = state_directory(stage)
    state.mkdir(parents=True)
    (state / "plan.json").write_text("{}")
    status = state / "status.json"
    status.write_text('{"status":"paused"}')
    with pytest.raises(ValueError, match="incompatible identity scheme"):
        bulk_convert("mls_sidon", root, output, resume=True)
    assert status.read_text() == '{"status":"paused"}'
