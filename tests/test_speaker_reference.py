import io
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf

from tts_data_pipeline.feature_audio import array_sha256
from tts_data_pipeline.schema import digest
from tts_data_pipeline.speaker.qwen3_ecapa.reference import boundary, sample, validate


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 24000, 44100, 48000, 11027])
def test_reference_constraints_and_retries(rate):
    jobs = [
        (int(t * rate), rate, str(i), 123)
        for i, t in enumerate([0.8, 1.0, 1.119, 1.12, 1.121, 2, 3, 10, 60, 120, 300])
    ]
    expected = [sample(*job) for job in jobs]
    with ThreadPoolExecutor(4) as pool:
        repeated = list(pool.map(lambda job: sample(*job), reversed(jobs)))
    assert repeated[::-1] == expected
    for (total, _, _, _), ref in zip(jobs, expected, strict=True):
        if ref is None:
            # Exhaust all intervals for these short boundary cases.
            full = total * 25 // (2 * rate)
            assert not any(
                max((rate + 1) // 2, (total + 9) // 10)
                <= boundary(b, rate) - boundary(a, rate)
                <= total // 2
                for a in range(full)
                for b in range(a + 1, full + 1)
            )
        else:
            a, b = ref["start_frame"], ref["end_frame"]
            assert 0 <= a < b <= total
            assert max((rate + 1) // 2, (total + 9) // 10) <= b - a <= total // 2
            assert a == boundary(ref["reference_codec_start"], rate)
            assert b == boundary(ref["reference_codec_end"], rate)


def test_nonprefix_and_seed():
    refs = [sample(240000, 24000, str(i), 123) for i in range(500)]
    assert any(r["reference_codec_start"] == 0 for r in refs)
    assert any(r["reference_codec_end"] == 125 for r in refs)
    assert len({r["reference_codec_end"] - r["reference_codec_start"] for r in refs}) > 40
    assert sample(240000, 24000, "one", 1) != sample(240000, 24000, "one", 2)


def test_frontend_crops_before_resampling():
    import hashlib

    import torch

    from tts_data_pipeline.speaker.qwen3_ecapa.frontend import decode_waveform, resampler

    rate = 44100
    original = np.random.default_rng(1).normal(0, 0.1, (rate * 3, 2)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, original, rate, format="WAV", subtype="FLOAT")
    raw = buffer.getvalue()
    ref = dict(
        sample(len(original), rate, "one", 123),
        reference_native_total_frames=len(original),
        reference_codec_feature_key="a" * 64,
        target_id="b" * 64,
        error_code=None,
    )
    row = dict(
        audio={"bytes": raw},
        audio_sha256=hashlib.sha256(raw).hexdigest(),
        sample_rate=rate,
        channels=2,
        num_frames=len(original),
        reference=ref,
    )
    wave, info, error = decode_waveform(row)
    expected = resampler(rate)(
        torch.from_numpy(original[ref["start_frame"] : ref["end_frame"]].mean(axis=1))
    )
    assert error is None and torch.equal(wave, expected)
    assert info["encoder_input_sha256"] == array_sha256(expected.numpy(), ["sample"])
    assert info["start_frame"] == ref["start_frame"]
    row["reference"] = dict(ref, reference_native_total_frames=len(original) + 1)
    assert decode_waveform(row)[2] == "reference_native_length_mismatch"


def test_reference_mapping_rejects_wrong_codec_and_offset():
    ref = sample(240000, 24000, "one", 123)
    codec = dict(
        target_id="a",
        audio_sha256="b",
        native_sample_rate=24000,
        start_frame=0,
        end_frame=240000,
        feature_key="c",
        num_codec_frames=125,
    )
    row = dict(
        codec,
        **{"start_frame": ref["start_frame"], "end_frame": ref["end_frame"]},
        reference_codec_start=ref["reference_codec_start"],
        reference_codec_end=ref["reference_codec_end"],
        reference_codec_feature_key="c",
        reference_native_total_frames=240000,
    )
    validate(row, codec)
    for change in [
        dict(reference_codec_start=ref["reference_codec_start"] + 1),
        dict(reference_codec_feature_key="wrong"),
        dict(start_frame=ref["start_frame"] + 1),
    ]:
        with pytest.raises(ValueError):
            validate(dict(row, **change), codec)


def test_reference_storage_and_merge():
    from test_merge_features import change, tables

    from tts_data_pipeline.merge_features import merge_tables
    from tts_data_pipeline.speaker.qwen3_ecapa.reference import FIELDS

    codec, speaker, text = tables()
    codec = change(codec, "end_frame", 0, 48000)
    codec = change(codec, "encoder_input_num_frames", 0, 48000)
    codec = change(codec, "num_codec_frames", 0, 25)
    codec = change(codec, "codes", 0, [[0] * 16] * 25).slice(0, 1)
    speaker, text = speaker.slice(0, 1), text.slice(0, 1)
    ref = sample(48000, 24000, "one", 123)
    speaker = change(speaker, "start_frame", 0, ref["start_frame"])
    speaker = change(speaker, "end_frame", 0, ref["end_frame"])
    speaker = change(speaker, "encoder_input_num_frames", 0, ref["end_frame"] - ref["start_frame"])
    values = dict(
        ref, reference_native_total_frames=48000, reference_codec_feature_key=digest([0, "key"])
    )
    for f in FIELDS:
        speaker = speaker.append_column(f, pa.array([values[f.name]], type=f.type))
    result, differences = merge_tables(codec, speaker, text, "example", "v0.1")
    assert not differences
    assert result["codec_codes"].equals(codec["codes"])
    assert result.select(text.schema.names).equals(text)
    assert result["speaker_reference_codec_start"][0].as_py() == ref["reference_codec_start"]


def test_reference_merge_filters_and_resumes(tmp_path):
    import json
    from pathlib import Path

    import lance
    from test_merge_features import fixture

    from tts_data_pipeline import merge_features as merge
    from tts_data_pipeline.feature_runtime import file_hash, write_json
    from tts_data_pipeline.speaker.qwen3_ecapa.reference import schema

    old = fixture(tmp_path)
    d = old["datasets"][0]
    codec = lance.dataset(d["sources"]["codec"]["table_path"]).to_table().to_pylist()
    speaker = lance.dataset(d["sources"]["speaker"]["table_path"]).to_table().to_pylist()
    by_id = {r["target_id"]: r for r in codec}
    for c in codec:
        c.update(
            end_frame=48000,
            encoder_input_num_frames=48000,
            num_codec_frames=25,
            codes=[[0] * 16] * 25,
        )
    by_id = {r["target_id"]: r for r in codec}
    for i, r in enumerate(speaker):
        c = by_id[r["target_id"]]
        ref = sample(48000, 24000, r["target_id"], 123)
        r.update(
            ref,
            reference_native_total_frames=48000,
            reference_codec_feature_key=c["feature_key"],
            encoder_input_num_frames=ref["end_frame"] - ref["start_frame"],
        )
        if i < 2:
            r.update(
                status="failed", error_code="test_failure", embedding=None, embedding_sha256=None
            )
    for kind, rows, arrow_schema in [
        ("codec", codec, lance.dataset(d["sources"]["codec"]["table_path"]).schema),
        ("speaker", speaker, schema(3)),
    ]:
        src = d["sources"][kind]
        ds = lance.write_dataset(
            pa.Table.from_pylist(rows, schema=arrow_schema), src["table_path"], mode="overwrite"
        )
        manifest = json.loads(Path(src["manifest_path"]).read_text())
        manifest["lance_version"] = ds.version
        if kind == "speaker":
            manifest["coverage"].update(ok=2, failed=2)
        write_json(src["manifest_path"], manifest)
    sp = tmp_path / "speaker.json"
    p = json.loads(sp.read_text())
    p["profile"] = {"reference": {}}
    write_json(sp, p)
    plan = merge.prepare(
        tmp_path / "codec.json",
        sp,
        tmp_path / "text.json",
        tmp_path / "reference-work",
        batch_rows=1,
    )
    hashes = {
        s["manifest_path"]: file_hash(s["manifest_path"])
        for s in plan["datasets"][0]["sources"].values()
    }
    merge.run(plan["work"], 1)
    final = Path(plan["datasets"][0]["final"])
    m = json.loads((final / "manifest.json").read_text())
    assert m["rows"] == 2 and m["selection"]["mode"] == "subset"
    assert m["reference_filtering"]["excluded_reasons"] == {"test_failure": 2}
    out = lance.dataset(final / "features.lance").to_table()
    assert set(out["target_id"].to_pylist()) == {
        r["target_id"] for r in speaker if r["status"] == "ok"
    }
    merge.run(plan["work"], 1)
    assert hashes == {path: file_hash(path) for path in hashes}


def test_saved_reference_plan_rejects_changed_sampling(tmp_path):
    import lance

    from tts_data_pipeline.feature_runtime import ordered_hash
    from tts_data_pipeline.speaker.qwen3_ecapa.reference import attach

    c = dict(
        target_id=digest("id"),
        audio_sha256=digest("audio"),
        status="ok",
        native_sample_rate=24000,
        start_frame=0,
        end_frame=240000,
        num_codec_frames=125,
        encoder_input_num_frames=240000,
        feature_key=digest("key"),
    )
    path = tmp_path / "codec.lance"
    ds = lance.write_dataset(pa.Table.from_pylist([c]), path)
    src = dict(table_path=str(path), lance_version=ds.version)
    row = dict(sample_id=c["target_id"], audio_sha256=c["audio_sha256"], sample_rate=24000)
    task = dict(id=ordered_hash([row["sample_id"]]), reference_offset=0)
    attach([row], src, task, tmp_path, 123, "example", "v0.1")
    first = dict(row["reference"])
    attach([row], src, task, tmp_path, 123, "example", "v0.1")
    assert row["reference"] == first
    with pytest.raises(ValueError, match="Saved reference plan changed"):
        attach([row], src, task, tmp_path, 124, "example", "v0.1")
