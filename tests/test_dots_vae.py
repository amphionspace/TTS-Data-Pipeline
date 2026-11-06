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


def value(frames, seed, target_id=None):
    g = np.random.default_rng(seed)
    padded = frames * model.HOP
    span = model.reference_patches(target_id or base_row(seed)["sample_id"], frames)
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
    reference = None
    if span is not None:
        info.update(
            reference_start_patch=span[0],
            reference_end_patch=span[1],
            reference_fbank_frames=(span[1] - span[0]) * 16,
        )
        reference = g.normal(size=512).astype(np.float32)
    return info, posterior, g.normal(size=512).astype(np.float32), reference, None


def test_table_roundtrip_keeps_arrays_text_and_failures():
    rows = [base_row(i) for i in range(3)]
    values = [
        value(28, 0),
        (dict(end_frame=48000), None, None, None, "silent_audio"),
        value(200, 2),
    ]
    table = make_table(rows, values, DEFINITION, "p" * 64)
    validate_table(table, model.REFERENCE_RULE)
    first = table.slice(0, 1).to_pylist()[0]
    assert np.array_equal(np.asarray(first["vae_mean"], np.float32), values[0][1][:, :128])
    assert np.array_equal(np.asarray(first["vae_logstd"], np.float32), values[0][1][:, 128:])
    assert first["num_latent_frames"] == 28 and first["text"] == rows[0]["text"]
    failed = table.slice(1, 1).to_pylist()[0]
    assert failed["status"] == "failed" and failed["vae_mean"] is None and failed["xvector"] is None
    assert failed["error_code"] == "silent_audio" and failed["reference_start_patch"] is None
    assert first["reference_start_patch"] is None and first["reference_xvector"] is None
    last = table.slice(2, 1).to_pylist()[0]
    span = (last["reference_start_patch"], last["reference_end_patch"])
    assert span == model.reference_patches(rows[2]["sample_id"], 200)
    assert np.array_equal(np.asarray(last["reference_xvector"], np.float32), values[2][3])


def test_reference_rule():
    import hashlib
    import math

    seen = set()
    for i in range(3000):
        target, frames = f"{i:064x}", 4 * (i % 300)
        n = frames // 4
        lo, hi = max(4, math.ceil(0.1 * n)), n // 2
        span = model.reference_patches(target, frames)
        if lo > hi:
            assert span is None
            continue
        h = hashlib.sha256(f"dots-reference-v1:{target}".encode()).digest()
        length = lo + int.from_bytes(h[0:8], "big") % (hi - lo + 1)
        start = int.from_bytes(h[8:16], "big") % (n - length + 1)
        assert span == (start, start + length)
        assert 0 <= span[0] < span[1] <= n and lo <= span[1] - span[0] <= hi
        seen.add(span[0] == 0)
    assert model.reference_patches("a" * 64, 7 * 4) is None
    start, end = model.reference_patches("a" * 64, 8 * 4)
    assert end - start == 4 and 0 <= start <= 4
    assert seen == {True, False}


def test_reference_rule_v2_lifts_the_minimum_to_3s():
    v2 = "dots-reference-v2:min-3s-on-patches"
    for i in range(3000):
        target, frames = f"{i:064x}", 4 * (i % 300)
        n = frames // 4
        v1, span = (
            model.reference_patches(target, frames),
            model.reference_patches(target, frames, v2),
        )
        assert (span is None) == (v1 is None)
        if span is not None:
            assert 0 <= span[0] < span[1] <= n
            assert min(19, n // 2) <= span[1] - span[0] <= n // 2
    # Under 6 s the span is half the utterance; v1 spans are unchanged.
    start, end = model.reference_patches("a" * 64, 30 * 4, v2)
    assert end - start == 15
    assert model.reference_patches("a" * 64, 8 * 4) == model.reference_patches(
        "a" * 64, 8 * 4, "dots-reference-v1:codec-grid-v1-on-patches"
    )


@pytest.mark.parametrize("damage", ["frames", "sha", "offsets", "span", "reference_sha"])
def test_validation_rejects_inconsistent_rows(damage):
    rows = [base_row(0)]
    info, posterior, xvector, reference, _ = value(200, 0)
    assert reference is not None
    if damage == "frames":
        posterior = posterior[:199]
        info = dict(info, padded_frames=199 * model.HOP)
    if damage == "span":
        info = dict(info, reference_start_patch=info["reference_start_patch"] + 1)
    table = make_table(rows, [(info, posterior, xvector, reference, None)], DEFINITION, "p" * 64)
    if damage == "reference_sha":
        table = table.set_column(
            table.schema.get_field_index("reference_xvector_sha256"),
            "reference_xvector_sha256",
            pa.array(["0" * 64]),
        )
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
        validate_table(table, model.REFERENCE_RULE)


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
    monkeypatch.setattr(
        run,
        "profile",
        lambda model_root, rule: dict(DEFINITION, model=str(model_root), reference_rule=rule),
    )
    monkeypatch.setattr(
        run, "read_base_manifest", lambda r: json.loads((r / "manifest.json").read_text())
    )
    p = run.prepare(root, "toy", tmp_path / "work", tmp_path, task_size=2)
    d = p["datasets"][0]
    assert d["target_count"] == 5 and len(d["tasks"]) == 3
    stage = tmp_path / "out"
    for task in d["tasks"]:
        prepared = run.read_task(d, task)
        values = [value(200, i, r["sample_id"]) for i, r in enumerate(prepared["rows"])]
        (run.Path(d["stage"])).mkdir(parents=True, exist_ok=True)
        run.write_task(p, d, task, prepared, values, {})
    manifest = run.finalize(p, d)
    assert manifest["selection"]["mode"] == "all_samples" and manifest["coverage"]["ok"] == 5
    published = lance.dataset(run.Path(d["final"]) / "features.lance")
    ids = np.asarray(published.to_table(columns=["target_id"])["target_id"].to_pylist(), "S64")
    assert sorted_set_hash(ids) == d["target_set_sha256"]
    validate_table(published.to_table(), model.REFERENCE_RULE)
    assert not stage.exists()


def test_selection_branch_publication_carries_selected_text(tmp_path, monkeypatch):
    from test_selection_filter import row as selection_row
    from test_selection_filter import write_base, write_parent
    from test_selection_revision import revision_rules, write_revision

    from tts_data_pipeline.selection import plan, publish, resolve_duplicates, scan, write_json

    root = tmp_path / "unified"
    wide = {
        "sample_rate": 48000,
        "num_frames": 96000,
        "channels": 1,
        "audio": {"bytes": b"x", "path": "a.wav"},
        "text_kind": "source_transcript_paralinguistic",
    }
    records = [
        selection_row(f"s{i}", text=f"好<crying>的{i}", language="zh") | wide for i in range(4)
    ]
    records[3] |= {"sample_rate": 16000, "num_frames": 32000}  # below the sample-rate rule
    base = write_base(root, "alpha", records)
    revised = [("ok", "replace", f"好[Crying]的{i}") for i in range(3)] + [("ok", "keep", None)]
    write_revision(root, "rev-run", base, records, revised)
    (root / "annotations/nv_tag_text/rev-run/manifest.json").write_text(
        json.dumps(
            json.loads((root / "annotations/nv_tag_text/rev-run/manifest.json").read_text())
            | {"profile": {"name": "nv-canonical-v1"}}
        )
    )
    write_parent(root, [])
    write_json(tmp_path / "rules.json", revision_rules())
    plan(root, tmp_path / "selection", tmp_path / "rules.json", selection_id="nv")
    scan(tmp_path / "selection", workers=1)
    resolve_duplicates(tmp_path / "selection")
    selection = publish(tmp_path / "selection", workers=1)
    manifest_path = root / "selections" / selection["selection_id"] / "manifest.json"
    monkeypatch.setattr(
        run, "profile", lambda model_root, rule: dict(DEFINITION, reference_rule=rule)
    )
    p = run.prepare_selection(root, manifest_path, tmp_path / "work", tmp_path, task_size=2)
    (d,) = p["datasets"]
    assert d["target_count"] == 3 and d["selection"]["text"]["text_kind"] == "nv-canonical-v1"
    for task in d["tasks"]:
        prepared = run.read_task(d, task)
        assert all(r["text"].startswith("好[Crying]") for r in prepared["rows"])
        assert {r["text_kind"] for r in prepared["rows"]} == {"nv-canonical-v1"}
        values = [value(28, 0, r["sample_id"]) for r in prepared["rows"]]
        run.Path(d["stage"]).mkdir(parents=True, exist_ok=True)
        run.write_task(p, d, task, prepared, values, {})
    manifest = run.finalize(p, d)
    assert manifest["selection"]["mode"] == "selection_branch"
    assert manifest["selection"]["filter"] == "selection_reason = 0"
    assert manifest["text"]["mapping_version"] == "nv-canonical-v1"
    assert manifest["inputs"][0]["branch"] == selection["outputs"][0]["branch"]
    texts = lance.dataset(run.Path(d["final"]) / "features.lance").to_table(columns=["text"])
    assert sorted(texts["text"].to_pylist()) == [f"好[Crying]的{i}" for i in range(3)]


def test_text_binding_names_asr_and_revision_sources(tmp_path):
    for kind, task, name in (
        ("annotation", "transcription", "qwen3-asr"),
        ("annotation_text_revision", "nv_tag_text", "nv-canonical-v1"),
    ):
        path = tmp_path / "annotations" / task / "r" / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"profile": {"name": name}}))
        selection = {
            "text_sources": {
                "0": {"kind": "base_normalization"},
                "1": {
                    "kind": kind,
                    "task": task,
                    "run_id": "r",
                    "manifest_sha256": run.file_hash(path),
                    "datasets": ["alpha"],
                },
            }
        }
        binding = run.text_binding(tmp_path, selection, "alpha")
        assert binding["text_kind"] == ("asr_transcription" if task == "transcription" else name)
        assert binding["profile_name"] == name
        assert run.text_binding(tmp_path, selection, "beta")["source"] == "base_normalization"
