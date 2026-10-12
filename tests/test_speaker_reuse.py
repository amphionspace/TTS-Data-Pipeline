import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf

from tts_data_pipeline.contract import codec_schema
from tts_data_pipeline.feature_runtime import file_hash, plan_dataset, write_json
from tts_data_pipeline.schema import base_schema, digest, make_record
from tts_data_pipeline.speaker.qwen3_ecapa import run
from tts_data_pipeline.speaker.qwen3_ecapa.frontend import decode_waveform

BASE_PROFILE = dict(
    kind="speaker_embedding", timeline_profile_id="a" * 64, output=dict(embedding_dim=4)
)
PROFILE = {**BASE_PROFILE, "reference": {"seed": 123}}
# Even rows are selected; the last one is too short for any legal reference.
LENGTHS = [72000 + 1920 * i for i in range(8)] + [24000]
SELECTED = {LENGTHS[i] for i in range(0, 9, 2)}


def fixture(tmp_path):
    rows = []
    for i, n in enumerate(LENGTHS):
        buffer = io.BytesIO()
        wave = np.random.default_rng(i).normal(0, 0.1, n).astype(np.float32)
        sf.write(buffer, wave, 24000, format="WAV", subtype="FLOAT")
        rows.append(
            make_record(
                dataset_id="example",
                source_snapshot="test",
                source_key=str(i),
                audio_bytes=buffer.getvalue(),
                audio_name=f"{i}.wav",
                source_locator={"test": i},
                text=str(i),
                text_kind="transcript",
                language="en-US",
            )
        )
    ds = lance.write_dataset(
        pa.Table.from_pylist(rows, schema=base_schema()),
        tmp_path / "samples.lance",
        max_rows_per_file=3,
    )
    ds.create_branch("selection-test", ds.version)
    branch = ds.checkout_version(("selection-test", ds.version))
    schema = pa.schema([("selection_reason", pa.uint16())])

    @lance.batch_udf(output_schema=schema)
    def selection(batch):
        return pa.RecordBatch.from_pylist(
            [
                {"selection_reason": 0 if n in SELECTED else 3001}
                for n in batch["num_frames"].to_pylist()
            ],
            schema=schema,
        )

    branch.add_columns(selection, read_columns=["num_frames"])
    source = dict(
        dataset_id="example",
        release_id="v0.1",
        table_path="samples.lance",
        branch="selection-test",
        lance_version=branch.version,
        base_version=ds.version,
        rows=9,
        selected_rows=5,
    )
    d = plan_dataset((tmp_path, source, 2, tmp_path))
    assert [t["rows"] for t in d["tasks"]] == [2, 1, 2]
    selection_path = tmp_path / "selection.json"
    write_json(selection_path, dict(status="complete", outputs=[source]))
    write_json(tmp_path / "manifest.json", dict(kind="base"))
    p = dict(
        root=str(tmp_path),
        output_root=str(tmp_path),
        profile=PROFILE,
        profile_id=digest(PROFILE),
        profile_name="test",
        selection_path=str(selection_path),
        selection_sha256=file_hash(selection_path),
        acceptance_sha256="4" * 64,
        execution_code_sha256="3" * 64,
        inputs=[
            dict(
                dataset_id="example",
                manifest_path="manifest.json",
                manifest_sha256=file_hash(tmp_path / "manifest.json"),
            )
        ],
    )
    selected = [r for r in rows if r["num_frames"] in SELECTED]
    return selected, p, d


def codec_run(tmp_path, selected, tag, d, lengths=None):
    """A published codec table over the selected targets; `lengths` override actual totals."""
    lengths = lengths or [r["num_frames"] for r in selected]
    final = (
        tmp_path / f"datasets/example/v0.1/features/codec/example-codec-{tag}-20261001T010000bjt-01"
    )
    records = []
    for r, n in zip(selected, lengths, strict=True):
        frames = (n + 1919) // 1920
        records.append(
            dict(
                target_kind="sample",
                target_id=r["sample_id"],
                parent_sample_id=r["sample_id"],
                feature_key=digest([r["sample_id"], tag]),
                input_fingerprint=digest([r["sample_id"], tag, "input"]),
                profile_id=digest(tag),
                audio_sha256=r["audio_sha256"],
                timeline_profile_id="t" * 64,
                status="ok",
                error_code=None,
                codes_sha256="d" * 64,
                encoder_input_sha256="e" * 64,
                start_frame=0,
                end_frame=n,
                native_sample_rate=24000,
                encoder_input_num_frames=n,
                num_codec_frames=frames,
                num_codebooks=16,
                codes=[[0] * 16] * frames,
            )
        )
    ds = lance.write_dataset(
        pa.Table.from_pylist(records, schema=codec_schema(16)), final / "features.lance"
    )
    ds.create_scalar_index("target_id", "BTREE")
    write_json(
        final / "manifest.json",
        dict(
            kind="codec",
            status="complete",
            run_id=final.name,
            profile_id=digest(tag),
            lance_version=ds.version,
            selection=dict(target_set_sha256=d["target_set_sha256"]),
        ),
    )
    plan = tmp_path / f"codec-{tag}.json"
    write_json(plan, dict(datasets=[dict(source=dict(dataset_id="example"), final=str(final))]))
    return plan


def make_run(tmp_path, d, name, codec_plan, reuse=None):
    """A speaker plan over the same targets, reading references from `codec_plan`."""
    final = (
        tmp_path
        / "datasets/example/v0.1/features/speaker_embedding"
        / f"example-speaker_embedding-test-{name}bjt-01"
    )
    item, target_set = run.codec_sources(codec_plan)["example"]
    assert target_set == d["target_set_sha256"]
    tasks = [dict(t) for t in d["tasks"]]
    offset = 0
    for t in tasks:
        t["reference_offset"] = offset
        offset += t["rows"]
    d2 = {
        **d,
        "tasks": tasks,
        "reference_codec_source": item,
        "run_id": final.name,
        "final": str(final),
        "stage": str(final) + ".incomplete",
        "state": str(tmp_path / f"state-{name}"),
        "reuse": reuse,
    }
    Path(d2["stage"]).mkdir(parents=True)
    (Path(d2["stage"]) / "execution.json").write_text("{}")
    return d2


class FakeEncoder:
    hardware = {"name": "fake"}

    def __init__(self):
        self.extracted = []

    def extract(self, rows, decoders):
        values = []
        for row in rows:
            self.extracted.append(row["sample_id"])
            wave, info, error = decode_waveform(row)
            if error:
                values.append((info, None, error))
                continue
            wave = wave.numpy()
            vector = np.array([wave.mean(), wave.std(), len(wave), 1.0], dtype=np.float32)
            values.append((info, vector, None))
        return values


def stored(d):
    ds = lance.dataset(Path(d["final"]) / "features.lance")
    return {r["target_id"]: r for r in ds.to_table().to_pylist()}


def publish_first(tmp_path):
    """Extract and publish the reference run every reuse test copies from."""
    selected, p, d = fixture(tmp_path)
    d1 = make_run(tmp_path, d, "20261001T020000", codec_run(tmp_path, selected, "old", d))
    encoder = FakeEncoder()
    with ThreadPoolExecutor(2) as decoders:
        checkpoints = [run.run_task(p, d1, t, encoder, decoders) for t in d1["tasks"]]
    assert sorted(encoder.extracted) == sorted(r["sample_id"] for r in selected)
    assert [c["reused_rows"] for c in checkpoints] == [0, 0, 0]
    assert all(c["reuse_source"] is None and c["hardware"] == {"name": "fake"} for c in checkpoints)
    manifest = run.finalize(p, d1)
    assert manifest["reuse"] is None
    assert manifest["coverage"]["ok"] == 4 and manifest["coverage"]["failed"] == 1
    assert manifest["error_counts"] == {"no_legal_reference": 1}
    assert manifest["execution"]["hardware"] == [{"name": "fake"}]
    assert not (Path(d1["final"]) / "execution.json").exists()
    return selected, p, d, d1, checkpoints


def set_globals(monkeypatch, p, d):
    monkeypatch.setattr(run, "_PLAN", p, raising=False)
    monkeypatch.setattr(run, "_SOURCES", {"example": d}, raising=False)


def assert_copied(old, new, codec_tag, except_ids=()):
    assert set(new) == set(old)
    for sid, row in new.items():
        assert row["reference_codec_feature_key"] == digest([sid, codec_tag])
        if sid in except_ids:
            continue
        unchanged = {k: v for k, v in old[sid].items() if k != "reference_codec_feature_key"}
        assert {k: row[k] for k in unchanged} == unchanged


def test_reuse_copies_embeddings_and_rebinds_codec_key(tmp_path, monkeypatch):
    selected, p, d, d1, first = publish_first(tmp_path)
    published = Path(d1["final"]) / "manifest.json"
    old = stored(d1)
    with pytest.raises(ValueError, match="profile"):
        run.reuse_sources([published], "f" * 64)
    [reuse] = run.reuse_sources([published], p["profile_id"])
    d2 = make_run(tmp_path, d, "20261002T010000", codec_run(tmp_path, selected, "new", d), reuse)
    set_globals(monkeypatch, p, d2)
    # Classification alone neither copies nor needs a GPU; copying needs no encoder.
    assert run.classify(("example", d2["tasks"][0], False)) == (
        "reusable",
        ("example", d2["tasks"][0]),
    )
    outcomes = [run.classify(("example", t, True)) for t in d2["tasks"]]
    assert [kind for kind, _ in outcomes] == ["checkpoint"] * 3
    checkpoints = [c for _, c in outcomes]
    assert sum(c["reused_rows"] for c in checkpoints) == 5
    assert all(
        c["hardware"] == {} and c["reuse_source"] == reuse["manifest_sha256"] for c in checkpoints
    )
    for key in ("audio_seconds", "reference_audio_seconds"):
        assert sum(c[key] for c in checkpoints) == pytest.approx(sum(c[key] for c in first))
    assert sum((c["error_counts"].get("no_legal_reference", 0) for c in checkpoints)) == 1
    with pytest.raises(ValueError, match="no encoder"):
        run.run_task(p, {**d2, "reuse": None}, d2["tasks"][0], None, None)
    manifest = run.finalize(p, d2)
    assert manifest["reuse"]["rows"] == 5
    assert manifest["reuse"]["manifest_sha256"] == reuse["manifest_sha256"]
    assert manifest["execution"]["hardware"] == []
    assert manifest["error_counts"] == {"no_legal_reference": 1}
    assert not (Path(d2["final"]) / "execution.json").exists()
    assert_copied(old, stored(d2), "new")


def test_mixed_task_extracts_only_missing_rows(tmp_path, monkeypatch):
    selected, p, d, d1, _ = publish_first(tmp_path)
    old = stored(d1)
    published = Path(d1["final"]) / "manifest.json"
    manifest = json.loads(published.read_text())
    table = lance.dataset(Path(d1["final"]) / "features.lance")
    missing = selected[0]["sample_id"]
    table.delete(f"target_id = '{missing}'")
    published.write_text(json.dumps({**manifest, "lance_version": table.version}))
    [reuse] = run.reuse_sources([published], p["profile_id"])
    d2 = make_run(tmp_path, d, "20261002T020000", codec_run(tmp_path, selected, "new", d), reuse)
    set_globals(monkeypatch, p, d2)
    outcomes = [run.classify(("example", t, True)) for t in d2["tasks"]]
    assert [kind for kind, _ in outcomes] == ["needs_gpu", "checkpoint", "checkpoint"]
    encoder = FakeEncoder()
    with ThreadPoolExecutor(2) as decoders:
        c = run.run_task(p, d2, d2["tasks"][0], encoder, decoders)
    assert encoder.extracted == [missing]
    assert (c["rows"], c["reused_rows"], c["hardware"]) == (2, 1, {"name": "fake"})
    manifest = run.finalize(p, d2)
    assert manifest["reuse"]["rows"] == 4
    assert manifest["execution"]["hardware"] == [{"name": "fake"}]
    assert_copied(old, stored(d2), "new")


def test_changed_reference_coordinates_are_recomputed(tmp_path, monkeypatch):
    selected, p, d, d1, _ = publish_first(tmp_path)
    old = stored(d1)
    [reuse] = run.reuse_sources([Path(d1["final"]) / "manifest.json"], p["profile_id"])
    # The pinned codec now reports a different actual length for one target, which moves
    # its reference; that row must be recomputed while every other row is copied.
    lengths = [r["num_frames"] for r in selected]
    lengths[3] -= 1920
    changed = selected[3]["sample_id"]
    codec = codec_run(tmp_path, selected, "new", d, lengths)
    d2 = make_run(tmp_path, d, "20261002T030000", codec, reuse)
    set_globals(monkeypatch, p, d2)
    outcomes = [run.classify(("example", t, True)) for t in d2["tasks"]]
    assert [kind for kind, _ in outcomes] == ["checkpoint", "checkpoint", "needs_gpu"]
    encoder = FakeEncoder()
    with ThreadPoolExecutor(2) as decoders:
        c = run.run_task(p, d2, d2["tasks"][2], encoder, decoders)
    assert encoder.extracted == [changed]
    assert c["reused_rows"] == 1 and c["error_counts"] == {
        "no_legal_reference": 1,
        "reference_native_length_mismatch": 1,
    }
    run.finalize(p, d2)
    new = stored(d2)
    assert_copied(old, new, "new", except_ids={changed})
    assert new[changed]["status"] == "failed"
    assert new[changed]["reference_native_total_frames"] == lengths[3]


def test_reusable_rule():
    ref = dict(
        target_id="t",
        reference_codec_feature_key="k",
        reference_native_total_frames=72000,
        error_code=None,
        start_frame=1920,
        end_frame=30720,
        reference_codec_start=1,
        reference_codec_end=16,
    )
    row = dict(audio_sha256="x", sample_rate=24000, reference=ref)
    old = dict(
        status="ok",
        error_code=None,
        audio_sha256="x",
        native_sample_rate=24000,
        start_frame=1920,
        end_frame=30720,
        reference_codec_start=1,
        reference_codec_end=16,
        reference_native_total_frames=72000,
    )
    assert run.reusable(old, row)
    assert not run.reusable(None, row)
    assert not run.reusable(old, {**row, "audio_sha256": "y"})
    assert not run.reusable(old, {**row, "sample_rate": 16000})
    assert not run.reusable({**old, "start_frame": 0}, row)
    assert not run.reusable({**old, "reference_codec_end": 17}, row)
    assert not run.reusable({**old, "status": "failed", "error_code": "audio_decode_failed"}, row)
    none = dict(
        ref,
        error_code="no_legal_reference",
        start_frame=0,
        end_frame=0,
        reference_codec_start=None,
        reference_codec_end=None,
    )
    stored_none = dict(
        old,
        status="failed",
        error_code="no_legal_reference",
        start_frame=0,
        end_frame=0,
        reference_codec_start=None,
        reference_codec_end=None,
    )
    assert run.reusable(stored_none, {**row, "reference": none})
    assert not run.reusable(old, {**row, "reference": none})
    assert not run.reusable(stored_none, row)
    assert not run.reusable(
        {**stored_none, "error_code": "audio_decode_failed"}, {**row, "reference": none}
    )


def test_prepare_pins_codec_plan_and_reuse_then_copies(tmp_path, monkeypatch):
    selected, p, d, d1, _ = publish_first(tmp_path)
    old = stored(d1)
    published = Path(d1["final"]) / "manifest.json"
    monkeypatch.setattr(
        run,
        "profile",
        lambda root, seed: (
            BASE_PROFILE if seed is None else {**BASE_PROFILE, "reference": {"seed": seed}}
        ),
    )
    acceptance = tmp_path / "acceptance.json"
    write_json(
        acceptance, dict(production_speaker_accepted=True, profile_id=p["profile_id"], evidence=[])
    )
    targets = tmp_path / "targets.json"
    write_json(
        targets,
        dict(
            root=p["root"],
            selection_path=p["selection_path"],
            selection_sha256=p["selection_sha256"],
            inputs=p["inputs"],
            datasets=[{k: d[k] for k in ("source", "target_count", "target_set_sha256", "tasks")}],
        ),
    )
    codec = codec_run(tmp_path, selected, "new", d)
    foreign = tmp_path / "foreign" / "manifest.json"
    (tmp_path / "foreign" / "features.lance").mkdir(parents=True)
    write_json(foreign, {**json.loads(published.read_text()), "profile_id": "f" * 64})
    with pytest.raises(ValueError, match="profile"):
        run.prepare(
            targets,
            tmp_path / "rejected",
            "models",
            acceptance,
            reference_codec_plan=codec,
            reference_seed=123,
            reuse=[foreign],
        )
    whole = tmp_path / "whole-sample-acceptance.json"
    write_json(
        whole, dict(production_speaker_accepted=True, profile_id=digest(BASE_PROFILE), evidence=[])
    )
    write_json(foreign, {**json.loads(published.read_text()), "profile_id": digest(BASE_PROFILE)})
    with pytest.raises(ValueError, match="reference profile"):
        run.prepare(targets, tmp_path / "rejected", "models", whole, reuse=[foreign])
    plan = run.prepare(
        targets,
        tmp_path / "work",
        "models",
        acceptance,
        reference_codec_plan=codec,
        reference_seed=123,
        reuse=[published],
    )
    assert plan["profile_id"] == p["profile_id"]
    assert plan["profile_name"] == "qwen3-ecapa-24k-fp32-reference-v1"
    [d2] = plan["datasets"]
    assert d2["state"] == str(tmp_path / "work/state/example")
    assert d2["reuse"]["manifest_sha256"] == file_hash(published)
    assert d2["reuse"]["table_path"] == str(Path(d1["final"]) / "features.lance")
    assert d2["reference_codec_source"]["run_id"].startswith("example-codec-new-")
    assert [t["reference_offset"] for t in d2["tasks"]] == [0, 2, 3]
    run.validate_plan(plan)
    original = published.read_text()
    published.write_text(original + "\n")
    with pytest.raises(ValueError, match="Reused speaker manifest changed"):
        run.validate_plan(plan)
    published.write_text(original)
    Path(d2["stage"]).mkdir(parents=True)
    set_globals(monkeypatch, plan, d2)
    outcomes = [run.classify(("example", t, True)) for t in d2["tasks"]]
    assert sum(c["reused_rows"] for _, c in outcomes) == 5
    manifest = run.finalize(plan, d2)
    assert manifest["reuse"]["rows"] == 5
    assert [i["alias"] for i in manifest["inputs"]] == ["samples", "reference_codec"]
    assert_copied(old, stored(d2), "new")
