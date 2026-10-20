import io
import json

import lance
import numpy as np
import pytest
import soundfile as sf
from scipy.signal import butter, sosfilt
from test_audio_stats import encoded, make_row, publish_fixture

from tts_data_pipeline.annotations.effective_bandwidth import metrics, run
from tts_data_pipeline.annotations.effective_bandwidth.schema import SCHEMA_VERSION
from tts_data_pipeline.feature_runtime import file_hash, write_json

RATE = 24000


def noise(seconds=2.0, cutoff=None, seed=0):
    x = np.random.default_rng(seed).standard_normal(int(seconds * RATE)) * 0.1
    if cutoff:
        x = sosfilt(butter(16, cutoff, fs=RATE, output="sos"), x)
    return x.astype(np.float32)[:, None]


def test_full_band_noise_reaches_nyquist_and_has_no_cliff():
    result = metrics.measure(noise(), RATE)
    assert result["segment_samples"] == 1024 and result["nyquist_hz"] == 12000
    assert result["edge_hz_50"] > 11500
    assert result["edge_drop_db"] is None  # no room above the edge
    assert result["band_db_8_12k"] > -6 and result["band_db_12_16k"] is None


def test_low_pass_shows_edge_and_sharp_drop():
    result = metrics.measure(noise(cutoff=8000), RATE)
    assert 7500 < result["edge_hz_40"] <= result["edge_hz_50"] <= result["edge_hz_60"] < 9500
    assert result["edge_drop_db"] > 20
    assert result["band_db_8_12k"] < result["band_db_4_8k"] - 30


def test_segment_length_tracks_sample_rate():
    assert [metrics.segment_samples(r) for r in (16000, 22050, 24000, 44100, 48000)] == [
        512,
        1024,
        1024,
        2048,
        2048,
    ]


def test_container_detection():
    assert metrics.container(b"ID3\x04") == "mp3"
    assert metrics.container(b"\xff\xfb\x90") == "mp3"
    assert metrics.container(b"fLaC") == "flac"
    assert metrics.container(encoded(np.zeros(10))) == "wav"
    assert metrics.container(b"\x00\x00\x00\x20ftypM4A ") == "mp4"
    assert metrics.container(b"junk") == "unknown"


def test_process_failures_keep_identity():
    good = make_row(encoded(noise(0.5)[:, 0], RATE), 1, rate=RATE)
    out = metrics.process(good, "profile")
    assert out["status"] == "ok" and out["result"]["container"] == "wav"
    assert out["result"]["num_samples_delta"] == RATE // 2 - 123
    assert out["result"]["encoded_kbps"] == pytest.approx(len(good["audio"]["bytes"]) * 16 / 1000)
    for data, code in [
        (b"bad", "audio_decode_failed"),
        (encoded(np.zeros(100), RATE), "audio_too_short"),
        (encoded(np.zeros(10000), RATE), "audio_silent"),
        (encoded(np.r_[np.zeros(5000), 1e-4, np.zeros(5000)], RATE), "audio_silent"),
    ]:
        out = metrics.process(make_row(data, 2, rate=RATE), "profile")
        assert (out["status"], out["error_code"], out["result"]) == ("failed", code, None)


def test_named_dataset_run_publishes_under_its_task(tmp_path):
    rows = [make_row(encoded(noise(0.5, seed=i)[:, 0], RATE), i, rate=RATE) for i in range(3)]
    root = publish_fixture(tmp_path, rows)
    with pytest.raises(ValueError, match="Not complete published bases"):
        run.prepare(root, tmp_path / "missing", ["other"])
    work = tmp_path / "work"
    plan = run.prepare(root, work, ["test"])
    assert plan["run_id"].startswith("tts-ann-effective_bandwidth-")
    assert plan["profile"]["task"] == "effective_bandwidth"
    assert "audio_stats/metrics.py" in plan["profile"]["decode_implementation"]
    (task,) = plan["tasks"]
    c = run.execute_task(plan, work, task, file_hash(work / "plan.json"))
    assert c["counts"] == {"ok": 3}
    write_json(work / "execution.json", dict(workers=1))
    path = run.publish(plan, work, {task["id"]: c})
    assert path.parent.parent.name == "effective_bandwidth"
    manifest = json.loads(path.read_text())
    assert manifest["task"] == "effective_bandwidth"
    assert manifest["schema_version"] == SCHEMA_VERSION
    table = manifest["outputs"][0]["table"]
    assert "annotations/effective_bandwidth" in table["table_path"]
    result = lance.dataset(root / table["table_path"], version=table["lance_version"]).to_table()
    assert all(r["result"]["edge_hz_50"] > 11000 for r in result.to_pylist())


def test_mp3_low_bitrate_edge_is_found():
    """A real low-bitrate MP3 when libsndfile can encode one (MPEG Layer III, 1.1+)."""
    stream = io.BytesIO()
    try:
        sf.write(
            stream,
            noise(2.0)[:, 0],
            RATE,
            format="MP3",
            subtype="MPEG_LAYER_III",
            compression_level=1.0,
        )
    except (sf.LibsndfileError, ValueError, TypeError):
        pytest.skip("libsndfile without MP3 encoding")
    wave, rate = sf.read(io.BytesIO(stream.getvalue()), dtype="float32", always_2d=True)
    result = metrics.measure(wave, rate)
    assert metrics.container(stream.getvalue()) == "mp3"
    assert result["edge_hz_50"] < 11500
