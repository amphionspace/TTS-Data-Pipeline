import hashlib
import io
import json

import lance
import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf

from tts_data_pipeline.annotations.audio_stats import metrics, run
from tts_data_pipeline.feature_runtime import file_hash, write_json
from tts_data_pipeline.schema import digest


def encoded(values, rate=1000):
    stream = io.BytesIO()
    sf.write(stream, values, rate, format="WAV", subtype="FLOAT")
    return stream.getvalue()


def test_stereo_preserves_energy_and_partial_window():
    values = np.zeros((83, 2), dtype=np.float32)
    values[20:60, 0] = 1.0
    values[20:60, 1] = -1.0
    m = metrics.measure(encoded(values))
    assert m["decoded_num_samples"] == 83
    assert m["rms"] == pytest.approx(np.sqrt(40 / 83))
    assert m["near_full_scale_ratio"] == pytest.approx(40 / 83)
    assert m["low_energy_intervals"] == [
        dict(start_sample=0, end_sample=20),
        dict(start_sample=60, end_sample=83),
    ]
    assert m["trailing_low_energy_samples"] == 23
    assert m["channel_stats"][0]["dc_offset"] == pytest.approx(40 / 83)
    assert m["channel_stats"][1]["dc_offset"] == pytest.approx(-40 / 83)


def test_long_silence_merges_blocks_and_nonfinite_rejected():
    m = metrics.measure(encoded(np.zeros(12001)))
    assert m["low_energy_intervals"] == [dict(start_sample=0, end_sample=12001)]
    assert m["low_energy_ratio"] == 1 and m["rms"] == 0
    with pytest.raises(metrics.InvalidAudio, match="audio_nonfinite"):
        metrics.measure(encoded(np.array([0, np.nan])))
    with pytest.raises(metrics.InvalidAudio, match="audio_empty"):
        metrics.measure(encoded(np.zeros(0)))


def test_streaming_matches_independent_whole_wave_reference():
    rng = np.random.default_rng(718)
    values = rng.uniform(-1.1, 1.1, size=(200123, 2)).astype(np.float32)
    wave = values.astype(np.float64)
    result = metrics.measure(encoded(values, 22050))
    assert result["rms"] == pytest.approx(np.sqrt(np.mean(wave**2)), abs=1e-12)
    assert result["peak"] == np.max(np.abs(wave))
    assert result["near_full_scale_ratio"] == np.mean(np.abs(wave) >= 0.999)
    assert result["at_or_above_full_scale_ratio"] == np.mean(np.abs(wave) >= 1)
    assert result["low_energy_intervals"] == []


def make_row(data, index):
    return dict(
        sample_id=digest(index),
        audio={"bytes": data},
        audio_sha256=hashlib.sha256(data).hexdigest(),
        sample_rate=1000,
        channels=1,
        num_frames=123,
    )


def test_bad_audio_and_header_difference_are_separate():
    row = make_row(encoded(np.zeros(121)), 1)
    target, result = metrics.process(row, "profile")
    assert target["status"] == "ok" and result["num_samples_delta"] == -2
    bad = make_row(b"bad", 2)
    target, result = metrics.process(bad, "profile")
    assert target["status"] == "failed" and result is None
    with pytest.raises(RuntimeError, match="hash conflict"):
        metrics.process(dict(row, audio_sha256="bad"), "profile")


@pytest.mark.parametrize("all_failed", [False, True])
def test_resumable_independent_publication(tmp_path, monkeypatch, all_failed):
    root = tmp_path / "unified"
    release = root / "datasets/test/v0.1"
    rows = [make_row(b"bad" if all_failed else encoded(np.zeros(123)), 0), make_row(b"bad", 1)]
    ds = lance.write_dataset(pa.Table.from_pylist(rows), release / "samples.lance")
    write_json(
        release / "manifest.json",
        dict(
            status="complete",
            artifact_kind="base",
            dataset_id="test",
            release_id="v0.1",
            rows=2,
            validation=dict(unique_ids=2),
            table_path="samples.lance",
            lance_version=ds.version,
        ),
    )
    work = tmp_path / "work"
    plan = run.prepare(root, work)
    task = plan["tasks"][0]
    checksum = file_hash(work / "plan.json")
    c = run.execute_task(plan, work, task, checksum)
    assert c["counts"]["failed"] == (2 if all_failed else 1)
    assert run.execute_task(plan, work, task, checksum) == c
    write_json(work / "execution.json", dict(workers=1))
    original_write = run.write_json

    def interrupt(path, value):
        if path.parent.name == "publications":
            raise RuntimeError("interrupted publication")
        original_write(path, value)

    monkeypatch.setattr(run, "write_json", interrupt)
    with pytest.raises(RuntimeError, match="interrupted publication"):
        run.publish(plan, work, {task["id"]: c})
    monkeypatch.setattr(run, "write_json", original_write)
    path = run.publish(plan, work, {task["id"]: c})
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "complete"
    assert manifest["outputs"][0]["coverage"]["missing"] == 0
    output = manifest["outputs"][0]
    assert manifest["schema_version"] == "audio-stats-v2"
    assert output["storage_kind"] == "sample_table" and "tables" not in output
    assert output["table"]["rows"] == 2
    result = (
        lance.dataset(
            root / output["table"]["table_path"], version=output["table"]["lance_version"]
        )
        .to_table()
        .to_pylist()
    )
    assert [r["sample_id"] for r in result] == [r["sample_id"] for r in rows]
    assert result[1]["status"] == "failed" and result[1]["result"] is None
    assert result[1]["error_code"] == "audio_decode_failed"
    assert (result[0]["result"] is None) == all_failed
    if not all_failed:
        assert result[0]["result"]["decoded_num_samples"] == 123
    assert not (root / output["table"]["table_path"]).with_name("targets.lance").exists()
    with pytest.raises(FileExistsError):
        run.publish(plan, work, {task["id"]: c})
    part = run.checkpoint_path(work, task) / "targets.parquet"
    part.write_bytes(b"bad")
    with pytest.raises(ValueError, match="file changed"):
        run.load_checkpoint(work, task, checksum)


def test_single_table_rejects_misaligned_checkpoint_payload(tmp_path):
    import pyarrow.parquet as pq

    from tts_data_pipeline.annotations.audio_stats.schema import result_schema
    from tts_data_pipeline.contract import annotation_targets_schema

    task = dict(id="test", rows=2)
    folder = run.checkpoint_path(tmp_path, task)
    folder.mkdir(parents=True)
    rows = [make_row(encoded(np.zeros(123)), i) for i in range(2)]
    outputs = [metrics.process(r, "profile") for r in rows]
    targets = pa.Table.from_pylist([t for t, _ in outputs], schema=annotation_targets_schema())
    results = pa.Table.from_pylist([r for _, r in outputs], schema=result_schema())
    pq.write_table(targets, folder / "targets.parquet")
    pq.write_table(results, folder / "results.parquet")
    combined = pa.Table.from_batches(list(run.annotation_batches(tmp_path, [task]))).to_pylist()
    assert [r["sample_id"] for r in combined] == [r["sample_id"] for r in rows]
    assert all(r["result"]["rms"] == 0 for r in combined)
    pq.write_table(results.take([1, 0]), folder / "results.parquet")
    with pytest.raises(ValueError, match="identity or fingerprint"):
        list(run.annotation_batches(tmp_path, [task]))


def test_publication_preserves_nulls_across_full_sized_checkpoint_slices(tmp_path):
    import pyarrow.parquet as pq

    from tts_data_pipeline.annotations.audio_stats.schema import result_schema
    from tts_data_pipeline.contract import annotation_targets_schema

    work = tmp_path / "work"
    task = dict(id="sliced-checkpoint", source=0, rows=8192)
    folder = run.checkpoint_path(work, task)
    folder.mkdir(parents=True)
    template_target, template_result = metrics.process(
        make_row(encoded(np.zeros(123)), 0), "profile"
    )
    targets, results = [], []
    failed = {1, 4097, 8191}
    for i in range(task["rows"]):
        identity = dict(target_id=digest(i), input_fingerprint=digest([i]))
        target = dict(template_target, **identity)
        if i in failed:
            target.update(status="failed", error_code="audio_nonfinite", item_count=0)
        else:
            results.append(dict(template_result, **identity))
        targets.append(target)
    pq.write_table(
        pa.Table.from_pylist(targets, schema=annotation_targets_schema()),
        folder / "targets.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist(results, schema=result_schema()), folder / "results.parquet"
    )
    expected = pa.Table.from_batches(list(run.annotation_batches(work, [task])))
    assert expected["result"].null_count == 3
    root = tmp_path / "unified"
    plan = dict(
        root=str(root),
        output_root=str(root),
        run_id="test-null-slices",
        profile={},
        profile_id="profile",
        scope="test",
        inputs=[dict(dataset_id="test", release_id="v0.1", alias="test/v0.1", target_rows=8192)],
        tasks=[task],
    )
    write_json(work / "execution.json", dict(test=True))
    manifest = json.loads(
        run.publish(
            plan,
            work,
            {task["id"]: dict(counts=dict(ok=8189, failed=3), errors=dict(audio_nonfinite=3))},
        ).read_text()
    )
    ref = manifest["outputs"][0]["table"]
    restored = lance.dataset(root / ref["table_path"], version=ref["lance_version"]).to_table()
    assert restored.equals(expected)
    assert restored["result"].null_count == 3
    assert [r["result"] for r in restored.take(sorted(failed)).to_pylist()] == [None] * 3
