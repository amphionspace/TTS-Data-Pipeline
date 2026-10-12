import hashlib
import io
import json
import os

import lance
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.annotations.audio_stats import metrics, run
from tts_data_pipeline.annotations.audio_stats.schema import SCHEMA_VERSION, annotation_schema
from tts_data_pipeline.feature_runtime import file_hash, write_json
from tts_data_pipeline.schema import digest


def encoded(values, rate=1000):
    stream = io.BytesIO()
    sf.write(stream, values, rate, format="WAV", subtype="FLOAT")
    return stream.getvalue()


def measure(values, rate=1000):
    values = np.asarray(values, dtype=np.float32)
    return metrics.measure(values if values.ndim == 2 else values[:, None], rate)


def reference(values, rate):
    """Whole-wave float64 reference written independently of the chunked implementation."""
    wave = np.asarray(values, dtype=np.float32)
    wave = wave if wave.ndim == 2 else wave[:, None]
    mono = wave.mean(axis=1, dtype=np.float32)
    x = mono.astype(np.float64)
    n, window = len(x), max(1, rate // 50)
    energy = np.array([np.mean(x[i : i + window] ** 2) for i in range(0, n, window)])
    low = []
    for i, flag in enumerate(energy <= 10 ** (-50 / 10)):
        start, end = i * window, min((i + 1) * window, n)
        if flag and low and low[-1][1] == start:
            low[-1][1] = end
        elif flag:
            low.append([start, end])
    zeros, run_start = [], None
    for i, value in enumerate(np.r_[mono, 1.0]):
        if value == 0 and run_start is None:
            run_start = i
        elif value != 0 and run_start is not None:
            zeros.append((run_start, i))
            run_start = None
    rms_windows = np.sort(np.sqrt(energy))
    return dict(
        decoded_num_samples=n,
        peak=np.abs(x).max(),
        rms=np.sqrt(np.mean(x**2)),
        dc_offset=np.mean(x),
        near_full_scale_ratio=np.mean(np.abs(x) >= 0.999),
        low_energy_ratio=sum(b - a for a, b in low) / n,
        leading_low_energy_samples=low[0][1] if low and low[0][0] == 0 else 0,
        trailing_low_energy_samples=n - low[-1][0] if low and low[-1][1] == n else 0,
        longest_low_energy_samples=max((b - a for a, b in low), default=0),
        longest_internal_low_energy_samples=max(
            (b - a for a, b in low if a > 0 and b < n), default=0
        ),
        window_rms_p10=rms_windows[int(np.floor(0.1 * (len(rms_windows) - 1)))],
        window_rms_p90=rms_windows[int(np.floor(0.9 * (len(rms_windows) - 1)))],
        longest_internal_zero_run_samples=max(
            (b - a for a, b in zeros if a > 0 and b < n), default=0
        ),
    )


def assert_matches_reference(values, rate):
    result, expected = measure(values, rate), reference(values, rate)
    for key, value in expected.items():
        assert result[key] == pytest.approx(value, rel=1e-12, abs=1e-15), key


def test_mono_mix_matches_codec_channel_mean():
    values = np.zeros((83, 2), dtype=np.float32)
    values[20:60, 0] = 1.0
    values[20:60, 1] = -1.0
    m = measure(values)
    assert m["source_channels"] == 2 and m["decoded_num_samples"] == 83
    assert m["peak"] == 0 and m["rms"] == 0 and m["low_energy_ratio"] == 1
    assert m["longest_internal_zero_run_samples"] == 0
    values[20:60, 1] = 0.0
    m = measure(values)
    assert m["peak"] == 0.5 and m["dc_offset"] == pytest.approx(20 / 83)
    assert m["near_full_scale_ratio"] == 0


def test_low_energy_intervals_and_zero_runs():
    rate, window = 1000, 20
    values = np.full(203, 0.5, dtype=np.float32)
    values[:40] = 0
    values[100:140] = 1e-4
    values[180:] = 0
    values[70:75] = 0
    m = measure(values, rate)
    assert m["leading_low_energy_samples"] == 2 * window
    assert m["trailing_low_energy_samples"] == 203 - 180
    assert m["longest_internal_low_energy_samples"] == 2 * window
    assert m["longest_low_energy_samples"] == 2 * window
    assert m["low_energy_ratio"] == pytest.approx((40 + 40 + 23) / 203)
    assert m["longest_internal_zero_run_samples"] == 5
    assert_matches_reference(values, rate)


def test_constant_full_scale_and_single_partial_window():
    m = measure(np.full(7, -1.0), 1000)
    assert m["near_full_scale_ratio"] == 1 and m["dc_offset"] == -1
    assert m["window_rms_p10"] == m["window_rms_p90"] == 1
    assert m["low_energy_ratio"] == 0 and m["longest_low_energy_samples"] == 0
    m = measure(np.zeros(1), 1)
    assert m["leading_low_energy_samples"] == m["trailing_low_energy_samples"] == 1
    assert m["longest_internal_low_energy_samples"] == 0


@pytest.mark.parametrize("rate", [1000, 22050, 44100])
def test_chunked_accumulation_matches_whole_wave_reference(monkeypatch, rate):
    monkeypatch.setattr(metrics, "CHUNK_WINDOWS", 3)
    rng = np.random.default_rng(rate)
    values = rng.uniform(-1.1, 1.1, size=(rate * 2 + 17, 2)).astype(np.float32)
    window = rate // 50
    values[window * 2 + 5 : window * 9] = 0
    values[window * 20 : window * 31] *= 1e-4
    assert_matches_reference(values, rate)


def test_invalid_waveforms_are_rejected():
    with pytest.raises(metrics.InvalidAudio, match="audio_nonfinite"):
        measure(np.array([0, np.nan]))
    with pytest.raises(metrics.InvalidAudio, match="audio_empty"):
        measure(np.zeros(0))


def test_decoder_warnings_counted_from_file_descriptor_two(monkeypatch):
    def noisy_read(stream, **kwargs):
        os.write(2, b"decoder warning one\ndecoder warning two\n")
        return np.zeros((3, 1), dtype=np.float32), 1000

    monkeypatch.setattr(metrics.sf, "read", noisy_read)
    assert metrics.decode(b"")[2] == 2
    assert metrics.decode(b"")[2] == 2


def make_row(data, index, num_frames=123, rate=1000, channels=1):
    return dict(
        sample_id=digest(index),
        audio={"bytes": data},
        audio_sha256=hashlib.sha256(data).hexdigest(),
        sample_rate=rate,
        channels=channels,
        num_frames=num_frames,
    )


def test_process_records_header_delta_and_failure_codes():
    row = make_row(encoded(np.zeros(121)), 1)
    out = metrics.process(row, "profile")
    assert out["status"] == "ok" and out["result"]["num_samples_delta"] == -2
    assert out["result"]["decoder_warnings"] == 0
    assert out["result"]["audio_sha256"] == row["audio_sha256"]
    for data, overrides, code in [
        (b"bad", {}, "audio_decode_failed"),
        (encoded(np.zeros(121)), dict(rate=2000), "native_format_mismatch"),
        (encoded(np.zeros((121, 2))), {}, "native_format_mismatch"),
        (encoded(np.array([0.0, np.inf])), {}, "audio_nonfinite"),
        (encoded(np.zeros(0)), {}, "audio_empty"),
    ]:
        out = metrics.process(make_row(data, 2, **overrides), "profile")
        assert (out["status"], out["error_code"], out["result"]) == ("failed", code, None)
    with pytest.raises(RuntimeError, match="hash conflict"):
        metrics.process(dict(row, audio_sha256="bad"), "profile")


def publish_fixture(tmp_path, rows):
    root = tmp_path / "unified"
    release = root / "datasets/test/v0.1"
    ds = lance.write_dataset(pa.Table.from_pylist(rows), release / "samples.lance")
    write_json(
        release / "manifest.json",
        dict(
            status="complete",
            artifact_kind="base",
            dataset_id="test",
            release_id="v0.1",
            rows=len(rows),
            validation=dict(unique_ids=len(rows)),
            table_path="samples.lance",
            lance_version=ds.version,
        ),
    )
    return root


@pytest.mark.parametrize("all_failed", [False, True])
def test_resumable_independent_publication(tmp_path, monkeypatch, all_failed):
    rows = [make_row(b"bad" if all_failed else encoded(np.zeros(123)), 0), make_row(b"bad", 1)]
    root = publish_fixture(tmp_path, rows)
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
    manifest = json.loads(run.publish(plan, work, {task["id"]: c}).read_text())
    assert manifest["status"] == "complete" and manifest["schema_version"] == SCHEMA_VERSION
    output = manifest["outputs"][0]
    assert output["storage_kind"] == "sample_table" and output["coverage"]["missing"] == 0
    table = output["table"]
    result = lance.dataset(root / table["table_path"], version=table["lance_version"])
    result = result.to_table().to_pylist()
    assert [r["sample_id"] for r in result] == [r["sample_id"] for r in rows]
    assert result[1]["status"] == "failed" and result[1]["result"] is None
    assert result[1]["error_code"] == "audio_decode_failed"
    assert (result[0]["result"] is None) == all_failed
    if not all_failed:
        assert result[0]["result"]["decoded_num_samples"] == 123
    with pytest.raises(FileExistsError):
        run.publish(plan, work, {task["id"]: c})
    (run.checkpoint_path(work, task) / "results.parquet").write_bytes(b"bad")
    with pytest.raises(ValueError, match="file changed"):
        run.load_checkpoint(work, task, checksum)


def test_publication_rejects_inconsistent_checkpoint(tmp_path):
    task = dict(id="test", rows=2)
    folder = run.checkpoint_path(tmp_path, task)
    folder.mkdir(parents=True)
    rows = [metrics.process(make_row(encoded(np.zeros(123)), i), "p") for i in range(2)]
    pq.write_table(
        pa.Table.from_pylist(rows, schema=annotation_schema()), folder / "results.parquet"
    )
    combined = pa.Table.from_batches(list(run.annotation_batches(tmp_path, [task]))).to_pylist()
    assert combined == rows
    rows[1] = dict(rows[1], status="failed", error_code="audio_nonfinite")
    pq.write_table(
        pa.Table.from_pylist(rows, schema=annotation_schema()), folder / "results.parquet"
    )
    with pytest.raises(ValueError, match="nullability"):
        list(run.annotation_batches(tmp_path, [task]))


def test_publication_preserves_nulls_across_full_sized_checkpoint_slices(tmp_path):
    work = tmp_path / "work"
    task = dict(id="sliced-checkpoint", source=0, rows=8192)
    folder = run.checkpoint_path(work, task)
    folder.mkdir(parents=True)
    template = metrics.process(make_row(encoded(np.zeros(123)), 0), "profile")
    failed = {1, 4097, 8191}
    rows = []
    for i in range(task["rows"]):
        row = dict(template, sample_id=digest(i), input_fingerprint=digest([i]))
        if i in failed:
            row.update(status="failed", error_code="audio_nonfinite", result=None)
        rows.append(row)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=annotation_schema()), folder / "results.parquet"
    )
    expected = pa.Table.from_batches(list(run.annotation_batches(work, [task])))
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
    counts = dict(counts=dict(ok=8189, failed=3), errors=dict(audio_nonfinite=3))
    manifest = json.loads(run.publish(plan, work, {task["id"]: counts}).read_text())
    ref = manifest["outputs"][0]["table"]
    restored = lance.dataset(root / ref["table_path"], version=ref["lance_version"]).to_table()
    assert restored.equals(expected)
    assert [r["result"] for r in restored.take(sorted(failed)).to_pylist()] == [None] * 3
