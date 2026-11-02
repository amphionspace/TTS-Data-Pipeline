"""dots_vae storage, validation and all_samples publication without the model weights."""

import json

import lance
import numpy as np
import pyarrow as pa
import pytest

from tts_data_pipeline.dots_vae import model, run
from tts_data_pipeline.dots_vae.profile import feature_row
from tts_data_pipeline.dots_vae.storage import make_table, validate_table
from tts_data_pipeline.feature_runtime import sorted_set_hash

DEFINITION = dict(timeline_profile_id="a" * 64, kind="dots_vae")


def base_row(i, frames=48000):
    return dict(
        sample_id=f"{i:064x}",
        audio_sha256=f"{i + 100:064x}",
        sample_rate=48000,
        num_frames=frames,
        channels=1,
        parent_sample_id=None,
        text=f"你好[Laughter]{i}",
        text_kind="source_asr_paralinguistic",
        language="zh",
    )


def value(frames, seed):
    g = np.random.default_rng(seed)
    padded = frames * model.HOP
    info = dict(
        end_frame=48000,
        resampled_frames=48000,
        edge_leading_delta=padded - 48000 - 100,
        edge_trailing_delta=100,
        processed_frames=padded,
        padded_frames=padded,
        xvector_fbank_frames=98,
    )
    posterior = g.normal(size=(frames, 256)).astype(np.float32)
    return info, posterior, g.normal(size=512).astype(np.float32), None


def test_table_roundtrip_keeps_arrays_text_and_failures():
    rows = [base_row(i) for i in range(3)]
    values = [value(28, 0), (dict(end_frame=48000), None, None, "silent_audio"), value(32, 1)]
    table = make_table(rows, values, DEFINITION, "p" * 64)
    validate_table(table)
    first = table.slice(0, 1).to_pylist()[0]
    assert np.array_equal(np.asarray(first["vae_mean"], np.float32), values[0][1][:, :128])
    assert np.array_equal(np.asarray(first["vae_logstd"], np.float32), values[0][1][:, 128:])
    assert first["num_latent_frames"] == 28 and first["text"] == rows[0]["text"]
    failed = table.slice(1, 1).to_pylist()[0]
    assert failed["status"] == "failed" and failed["vae_mean"] is None and failed["xvector"] is None
    assert failed["error_code"] == "silent_audio"


@pytest.mark.parametrize("damage", ["frames", "sha", "offsets"])
def test_validation_rejects_inconsistent_rows(damage):
    rows = [base_row(0)]
    info, posterior, xvector, _ = value(28, 0)
    if damage == "frames":
        posterior = posterior[:27]
        info = dict(info, padded_frames=27 * model.HOP)
    table = make_table(rows, [(info, posterior, xvector, None)], DEFINITION, "p" * 64)
    if damage == "sha":
        table = table.set_column(
            table.schema.get_field_index("xvector_sha256"),
            "xvector_sha256",
            pa.array(["0" * 64]),
        )
    if damage == "offsets":
        table = table.set_column(
            table.schema.get_field_index("edge_trailing_delta"),
            "edge_trailing_delta",
            pa.array([5], pa.int64()),
        )
    with pytest.raises(ValueError):
        validate_table(table)


def test_feature_key_ignores_text_but_binds_audio_and_profile():
    a = feature_row(base_row(0), DEFINITION, *value(28, 0)[:1], None, "p" * 64)
    b = feature_row(dict(base_row(0), text="x"), DEFINITION, *value(28, 0)[:1], None, "p" * 64)
    c = feature_row(base_row(0), DEFINITION, *value(28, 0)[:1], None, "q" * 64)
    assert a["feature_key"] == b["feature_key"] != c["feature_key"]


def test_all_samples_publication(tmp_path, monkeypatch):
    root = tmp_path / "root"
    release = root / "datasets/toy/v0.1"
    rows = [base_row(i) for i in range(5)]
    lance.write_dataset(
        pa.Table.from_pylist([dict(r, audio={"bytes": b"x", "path": "a.wav"}) for r in rows]),
        release / "samples.lance",
        max_rows_per_file=3,
    )
    (release / "manifest.json").write_text(
        json.dumps(dict(status="complete", rows=5, table_path="samples.lance", lance_version=1))
    )
    monkeypatch.setattr(run, "profile", lambda model_root: dict(DEFINITION, model=str(model_root)))
    monkeypatch.setattr(
        run, "read_base_manifest", lambda r: json.loads((r / "manifest.json").read_text())
    )
    p = run.prepare(root, "toy", tmp_path / "work", tmp_path, task_size=2)
    d = p["datasets"][0]
    assert d["target_count"] == 5 and len(d["tasks"]) == 3
    stage = tmp_path / "out"
    for task in d["tasks"]:
        prepared = run.read_task(d, task)
        values = [value(28, i) for i in range(task["rows"])]
        (run.Path(d["stage"])).mkdir(parents=True, exist_ok=True)
        run.write_task(p, d, task, prepared, values, {})
    manifest = run.finalize(p, d)
    assert manifest["selection"]["mode"] == "all_samples" and manifest["coverage"]["ok"] == 5
    published = lance.dataset(run.Path(d["final"]) / "features.lance")
    ids = np.asarray(published.to_table(columns=["target_id"])["target_id"].to_pylist(), "S64")
    assert sorted_set_hash(ids) == d["target_set_sha256"]
    validate_table(published.to_table())
    assert not stage.exists()
