from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pytest
from test_speaker_reuse import (
    PROFILE,
    FakeEncoder,
    codec_run,
    fixture,
    make_run,
    stored,
)

from tts_data_pipeline.schema import digest
from tts_data_pipeline.speaker.qwen3_ecapa import prefix_reference, reference, run

PREFIX_PROFILE = {
    **PROFILE,
    "reference": {"seed": 123, "version": prefix_reference.POLICY_VERSION},
}


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 24000, 44100, 48000])
def test_prefix_keeps_uniform_lengths_and_starts_at_zero(rate):
    for i, seconds in enumerate([0.8, 1.0, 1.119, 1.12, 2, 3, 10, 60, 300] * 20):
        total = int(seconds * rate) + i
        uniform = reference.sample(total, rate, str(i), 123)
        prefix = prefix_reference.sample(total, rate, str(i), 123)
        if uniform is None:
            assert prefix is None
            continue
        assert prefix["reference_codec_start"] == prefix["start_frame"] == 0
        assert (
            prefix["reference_codec_end"]
            == uniform["reference_codec_end"] - uniform["reference_codec_start"]
        )
        assert prefix["end_frame"] == uniform["end_frame"] - uniform["start_frame"]
        assert prefix["end_frame"] == reference.boundary(prefix["reference_codec_end"], rate)


def test_prefix_rejects_non_integral_rates():
    with pytest.raises(NotImplementedError):
        prefix_reference.sample(110270, 11027, "x", 123)


def test_prefix_validate_requires_start_zero():
    rate, total = 24000, 240000
    codec = dict(
        native_sample_rate=rate,
        end_frame=total,
        feature_key="k",
        audio_sha256="a",
        target_id="t",
        start_frame=0,
        num_codec_frames=125,
    )
    identities = (str(i) for i in range(1000))
    uniform = next(
        r
        for r in (reference.sample(total, rate, i, 123) for i in identities)
        if r["reference_codec_start"]
    )
    row = dict(
        uniform,
        reference_codec_feature_key="k",
        reference_native_total_frames=total,
        native_sample_rate=rate,
        audio_sha256="a",
        target_id="t",
    )
    reference.validate(row, codec)
    with pytest.raises(ValueError, match="first frame"):
        prefix_reference.validate(row, codec)
    row.update(prefix_reference.shift(uniform, rate))
    prefix_reference.validate(row, codec)


def test_prefix_profile_is_distinct(monkeypatch):
    from tts_data_pipeline.speaker.qwen3_ecapa import profile

    monkeypatch.setattr(
        profile,
        "profile",
        lambda root, seed: dict(
            reference=reference.policy(seed),
            implementation=dict(sources_sha256={}),
            waveform=dict(order=["decode_native", "crop_codec_grid_reference", "resample"]),
        ),
    )
    definition = run.speaker_profile("models", 123, "prefix")
    assert prefix_reference.is_prefix(definition)
    assert run.placement(definition) == "prefix"
    assert definition["reference"]["seed"] == 123
    assert definition["waveform"]["order"][1] == "crop_codec_grid_prefix_reference"
    assert "prefix_reference" in definition["implementation"]["sources_sha256"]
    assert run.placement(profile.profile("models", 123)) == "uniform"


def test_prefix_run_matches_uniform_lengths(tmp_path):
    """A prefix run keeps its own uniform plan's statuses and lengths, every start at 0."""
    selected, p, d = fixture(tmp_path / "prefix")
    p = {**p, "profile": PREFIX_PROFILE, "profile_id": digest(PREFIX_PROFILE)}
    d2 = make_run(
        tmp_path / "prefix",
        d,
        "20261002T020000",
        codec_run(tmp_path / "prefix", selected, "old", d),
    )
    encoder = FakeEncoder()
    with ThreadPoolExecutor(2) as decoders:
        for t in d2["tasks"]:
            run.run_task(p, d2, t, encoder, decoders)
    manifest = run.finalize(p, d2)
    assert manifest["error_counts"] == {"no_legal_reference": 1}
    prefix = stored(d2)
    # Lengths come from the uniform draw for the same identity (its audio bytes included),
    # so compare with the uniform plan this run kept, not with another fixture's run.
    state = Path(d2["state"])
    uniform = {}
    for t in d2["tasks"]:
        with pa.ipc.open_file(state / "uniform" / "references" / f"{t['id']}.arrow") as reader:
            uniform.update({r["target_id"]: r for r in reader.read_all().to_pylist()})
    assert set(prefix) == set(uniform)
    for sid, row in prefix.items():
        old = uniform[sid]
        assert (row["status"] == "ok") == (old["error_code"] is None)
        if row["status"] != "ok":
            continue
        assert row["reference_codec_start"] == row["start_frame"] == 0
        assert row["reference_codec_end"] == (
            old["reference_codec_end"] - old["reference_codec_start"]
        )
        assert row["end_frame"] == old["end_frame"] - old["start_frame"]
    for t in d2["tasks"]:
        assert (state / "references" / f"{t['id']}.arrow").exists()
        assert (state / "uniform" / "references" / f"{t['id']}.arrow").exists()


def test_prefix_merge_rejects_shifted_reference():
    from test_merge_features import change, tables

    from tts_data_pipeline.merge_features import merge_tables

    codec, speaker, text = tables()
    codec = change(codec, "end_frame", 0, 240000)
    codec = change(codec, "encoder_input_num_frames", 0, 240000)
    codec = change(codec, "num_codec_frames", 0, 125)
    codec = change(codec, "codes", 0, [[0] * 16] * 125).slice(0, 1)
    speaker, text = speaker.slice(0, 1), text.slice(0, 1)
    uniform = next(
        r
        for r in (reference.sample(240000, 24000, str(i), 123) for i in range(1000))
        if r["reference_codec_start"]
    )

    def with_reference(ref):
        s = change(speaker, "start_frame", 0, ref["start_frame"])
        s = change(s, "end_frame", 0, ref["end_frame"])
        s = change(s, "encoder_input_num_frames", 0, ref["end_frame"] - ref["start_frame"])
        values = dict(
            ref,
            reference_native_total_frames=240000,
            reference_codec_feature_key=digest([0, "key"]),
        )
        for f in reference.FIELDS:
            s = s.append_column(f, pa.array([values[f.name]], type=f.type))
        return s

    shifted = with_reference(uniform)
    merge_tables(codec, shifted, text, "example", "v0.1")
    with pytest.raises(ValueError, match="first frame"):
        merge_tables(codec, shifted, text, "example", "v0.1", prefix=True)
    result, _ = merge_tables(
        codec,
        with_reference(prefix_reference.shift(uniform, 24000)),
        text,
        "example",
        "v0.1",
        prefix=True,
    )
    assert result["speaker_reference_codec_start"][0].as_py() == 0
