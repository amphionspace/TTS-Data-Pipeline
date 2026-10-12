import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf

from tts_data_pipeline import feature_runtime
from tts_data_pipeline.codec.qwen3_12hz import run
from tts_data_pipeline.feature_contract import target_set_sha256
from tts_data_pipeline.schema import base_schema, digest, make_record

TEXT_SOURCES = {
    "0": {
        "kind": "base_normalization",
        "normalization": {
            "name": "unicode-strip-v1",
            "implementation": "python.str.strip",
            "python_version": "3.11",
        },
    }
}


def fixture(tmp_path, frame_hint_offset=0):
    rows = []
    for i in range(9):
        buffer = io.BytesIO()
        sf.write(
            buffer,
            np.full(24000 + i, i / 100, dtype=np.float32),
            24000,
            format="WAV",
            subtype="FLOAT",
        )
        rows.append(
            make_record(
                dataset_id="example",
                source_snapshot="test",
                source_key=str(i),
                audio_bytes=buffer.getvalue(),
                audio_name=f"{i}.wav",
                source_locator={"test": i},
                text=f" {i} ",
                text_kind="transcript",
                language="en-US",
            )
        )
    for row in rows:
        row["num_frames"] += frame_hint_offset
    ds = lance.write_dataset(
        pa.Table.from_pylist(rows, schema=base_schema()),
        tmp_path / "samples.lance",
        max_rows_per_file=3,
    )
    ds.create_branch("selection-test", ds.version)
    branch = ds.checkout_version(("selection-test", ds.version))
    schema = pa.schema(
        [
            ("selection_reason", pa.uint16()),
            ("selected_text", pa.string()),
            ("selected_text_source", pa.uint32()),
            ("selected_language", pa.string()),
        ]
    )

    @lance.batch_udf(output_schema=schema)
    def selection(batch):
        return pa.RecordBatch.from_pylist(
            [
                {
                    "selection_reason": 0 if n % 2 == 0 else 3001,
                    "selected_text": str(n - 24000),
                    "selected_text_source": 0,
                    "selected_language": "en",
                }
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
    d = run.plan_dataset((tmp_path, source, 1, tmp_path))
    final = (
        tmp_path / "datasets/example/v0.1/features/codec/example-codec-test-20260929T010000bjt-01"
    )
    d.update(
        stage=str(final) + ".incomplete",
        final=str(final),
        state=str(tmp_path / "state"),
        run_id=final.name,
    )
    Path(d["stage"]).mkdir(parents=True)
    profile = {"kind": "audio_codec", "timeline_profile_id": "1" * 64}
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({"text_sources": TEXT_SOURCES}))
    p = dict(
        root=str(tmp_path),
        output_root=str(tmp_path),
        profile=profile,
        profile_id=digest(profile),
        profile_name="test",
        selection_path=str(selection_path),
        selection_sha256="2" * 64,
        execution_code_sha256="3" * 64,
        text_code_sha256="6" * 64,
        acceptance_sha256="4" * 64,
        inputs=[
            {"dataset_id": "example", "manifest_path": "manifest.json", "manifest_sha256": "5" * 64}
        ],
    )
    return rows, p, d


class FakeEncoder:
    def encode_many(self, waves):
        return [
            np.full(((len(w) + 1919) // 1920, 16), int(w[0] * 100), dtype=np.int16) for w in waves
        ]


def setup_worker(monkeypatch, p, d, decoders):
    monkeypatch.setattr(run, "_PLAN", p, raising=False)
    monkeypatch.setattr(run, "_SOURCES", {"example": d}, raising=False)
    monkeypatch.setattr(run, "_ENCODER", FakeEncoder(), raising=False)
    monkeypatch.setattr(run, "_DECODERS", decoders, raising=False)


def test_checkpoint_resume_and_corruption(tmp_path, monkeypatch):
    _, p, d = fixture(tmp_path)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        completed = [run.run_task(("example", task)) for task in reversed(d["tasks"])]
        assert sum(c["rows"] for c in completed) == 5
        monkeypatch.setattr(run, "_ENCODER", None)
        assert run.run_task(("example", d["tasks"][-1])) == completed[0]
        path = Path(d["stage"]) / "features.lance/data" / completed[0]["files"][0]["name"]
        with path.open("ab") as stream:
            stream.write(b"corrupt")
        with pytest.raises(ValueError, match="corrupted"):
            run.run_task(("example", d["tasks"][-1]))


def test_target_set_digest_matches_contract():
    ids = [digest([i]) for i in range(3)]
    expected = target_set_sha256(
        [dict(target_kind="sample", target_id=i, parent_sample_id=i) for i in sorted(ids)]
    )
    assert run.sorted_set_hash(np.asarray(ids, dtype="S64")) == expected
    with pytest.raises(ValueError, match="Duplicate"):
        run.sorted_set_hash(np.asarray([ids[0], ids[0]], dtype="S64"))


def test_actual_frames_reach_checkpoint_and_stored_identity(tmp_path, monkeypatch):
    from lance.file import LanceFileReader

    _, p, d = fixture(tmp_path, frame_hint_offset=2)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        for task in d["tasks"]:
            checkpoint = run.run_task(("example", task))
            assert len(checkpoint["frame_count_mismatches"]) == task["rows"]
            for entry in checkpoint["frame_count_mismatches"]:
                assert entry["base_frames"] == entry["decoded_frames"] + 2
            for entry in checkpoint["files"]:
                path = Path(d["stage"]) / "features.lance/data" / entry["name"]
                for batch in LanceFileReader(str(path)).read_all().to_batches():
                    for row in batch.to_pylist():
                        run.validate_result(row)
                        assert row["end_frame"] == row["encoder_input_num_frames"]
                        assert checkpoint["audio_seconds"] == row["end_frame"] / 24000


def test_publish_recovery_text_and_flat_run_path(tmp_path, monkeypatch):
    _, p, d = fixture(tmp_path)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        for task in d["tasks"]:
            run.run_task(("example", task))
    orphan = Path(d["stage"]) / "features.lance/data/.tmp-abandoned"
    orphan.write_bytes(b"abandoned")
    rename = Path.rename

    def crash_before_publish(self, target):
        if self == Path(d["stage"]):
            raise RuntimeError("simulated publication crash")
        return rename(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", crash_before_publish)
        with pytest.raises(RuntimeError, match="simulated"):
            run.finalize(p, d)
    assert not orphan.exists()
    manifest = run.finalize(p, d)
    assert run.published_manifest(p, d) == manifest
    assert manifest["table_path"] == f"features/codec/{d['run_id']}/features.lance"
    ds = lance.dataset(Path(d["final"]) / "features.lance", version=manifest["lance_version"])
    assert ds.to_table(columns=["text"])["text"].to_pylist() == ["0", "2", "4", "6", "8"]
    assert ds.to_table(columns=["language"])["language"].to_pylist() == ["en"] * 5
    assert manifest["text_materialization"]["original_codec_files_preserved"]
    assert {"target_id", "feature_key"} <= {f for i in ds.list_indices() for f in i["fields"]}
    assert ".tmp-abandoned" in (Path(d["final"]) / "cleanup.jsonl").read_text()
    with pytest.raises(ValueError, match="differs"):
        run.published_manifest({**p, "profile_id": "9" * 64}, d)


def rerun(p, d, tmp_path, name, profile):
    """A second plan over the same targets, writing to a fresh run directory."""
    final = tmp_path / f"datasets/example/v0.1/features/codec/example-codec-test-{name}bjt-01"
    d2 = {
        **d,
        "tasks": [dict(t) for t in d["tasks"]],
        "stage": str(final) + ".incomplete",
        "final": str(final),
        "state": str(tmp_path / f"state-{name}"),
        "run_id": final.name,
    }
    Path(d2["stage"]).mkdir(parents=True)
    return {**p, "profile": profile, "profile_id": digest(profile)}, d2


def stored(d, columns):
    ds = lance.dataset(Path(d["final"]) / "features.lance")
    return {r["target_id"]: r for r in ds.to_table(columns=["target_id", *columns]).to_pylist()}


def test_reuse_copies_payloads_and_rebinds_identity(tmp_path, monkeypatch):
    _, p, d = fixture(tmp_path)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        for task in d["tasks"]:
            run.run_task(("example", task))
    run.finalize(p, d)
    published = Path(d["final"]) / "manifest.json"
    old = stored(d, ["codes", "feature_key", "end_frame"])
    with pytest.raises(ValueError, match="not accepted"):
        run.reuse_sources(tmp_path, [published], "f" * 64, {})
    for name, profile in [
        ("20261012T010000", p["profile"]),
        ("20261012T020000", {"kind": "audio_codec", "timeline_profile_id": "7" * 64}),
    ]:
        p2, d2 = rerun(p, d, tmp_path, name, profile)
        [d2["reuse"]] = run.reuse_sources(
            tmp_path, [published], p2["profile_id"], {"predecessor_profile_id": p["profile_id"]}
        )
        run.plan_reuse(p2, d2)
        assert all(t["reuse_rows"] == t["rows"] for t in d2["tasks"])
        # No encoder: every target must come from the published run.
        setup_worker(monkeypatch, p2, d2, None)
        monkeypatch.setattr(run, "_ENCODER", None)
        assert sum(run.run_task(("example", t))["reused_rows"] for t in d2["tasks"]) == 5
        manifest = run.finalize(p2, d2)
        assert manifest["reuse"]["rows"] == 5
        assert not (Path(d2["final"]) / "execution.json").exists()
        new = stored(d2, ["codes", "feature_key", "profile_id", "end_frame"])
        assert {k: v["codes"] for k, v in new.items()} == {k: v["codes"] for k, v in old.items()}
        assert all(v["profile_id"] == p2["profile_id"] for v in new.values())
        same_keys = all(new[k]["feature_key"] == old[k]["feature_key"] for k in old)
        assert same_keys == (profile is p["profile"])


def test_reuse_requires_same_audio_and_matching_timeline():
    old = dict(status="ok", audio_sha256="x", native_sample_rate=24000, end_frame=100)
    row = dict(audio_sha256="x", sample_rate=24000, num_frames=101)
    # A header/actual mismatch can only be reused within the same profile.
    assert run.reusable(old, row, same_profile=True)
    assert not run.reusable(old, row, same_profile=False)
    assert run.reusable(old, {**row, "num_frames": 100}, same_profile=False)
    assert not run.reusable({**old, "status": "failed"}, row, same_profile=True)
    assert not run.reusable(old, {**row, "audio_sha256": "y"}, same_profile=True)
    assert not run.reusable(None, row, same_profile=True)


def test_reuse_and_encoding_share_a_run(tmp_path, monkeypatch):
    _, p, d = fixture(tmp_path)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        for task in d["tasks"]:
            run.run_task(("example", task))
        run.finalize(p, d)
        published = Path(d["final"]) / "manifest.json"
        manifest = json.loads(published.read_text())
        # Drop one stored target so its task must be encoded again.
        table = lance.dataset(Path(d["final"]) / "features.lance")
        missing = table.to_table(columns=["target_id"])["target_id"][0].as_py()
        table.delete(f"target_id = '{missing}'")
        published.write_text(json.dumps({**manifest, "lance_version": table.version}))
        p2, d2 = rerun(p, d, tmp_path, "20261012T030000", p["profile"])
        [d2["reuse"]] = run.reuse_sources(tmp_path, [published], p2["profile_id"], {})
        run.plan_reuse(p2, d2)
        assert sorted(t["reuse_rows"] for t in d2["tasks"]) == [0, 1, 1, 1, 1]
        setup_worker(monkeypatch, p2, d2, decoders)
        checkpoints = [run.run_task(("example", t)) for t in d2["tasks"]]
    assert sum(c["reused_rows"] for c in checkpoints) == 4
    assert sum(c["rows"] for c in checkpoints) == 5
    run.finalize(p2, d2)
    assert stored(d2, ["codes"]) == stored(d, ["codes"]) | {missing: stored(d2, ["codes"])[missing]}


def test_undecodable_sample_is_a_failed_row_not_a_task_abort(tmp_path, monkeypatch):
    rows, p, d = fixture(tmp_path)
    bad = d["tasks"][0]
    real = run.decode

    def decode(row):
        if row["sample_id"] == rows[0]["sample_id"]:
            raise ValueError("native_format_mismatch: injected")
        return real(row)

    monkeypatch.setattr(run, "decode", decode)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        for task in d["tasks"]:
            run.run_task(("example", task))
    run.finalize(p, d)
    ds = lance.dataset(Path(d["final"]) / "features.lance")
    status = {
        r["target_id"]: (r["status"], r["error_code"])
        for r in ds.to_table(columns=["target_id", "status", "error_code"]).to_pylist()
    }
    assert status.pop(rows[0]["sample_id"]) == ("failed", "native_format_mismatch")
    assert set(status.values()) == {("ok", None)} and bad["rows"] == 1


def test_lost_file_after_commit_is_redone_and_published(tmp_path, monkeypatch):
    _, p, d = fixture(tmp_path)
    (Path(d["stage"]) / "execution.json").write_text("{}")
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        checkpoints = [run.run_task(("example", task)) for task in d["tasks"]]
        lost = Path(d["stage"]) / "features.lance/data" / checkpoints[0]["files"][0]["name"]
        commit = lance.LanceDataset.commit

        def commit_then_lose(*args, **kwargs):
            ds = commit(*args, **kwargs)
            lost.unlink()  # What a lost quarkfs rename leaves behind.
            return ds

        with monkeypatch.context() as patch:
            patch.setattr(lance.LanceDataset, "commit", staticmethod(commit_then_lose))
            with pytest.raises(ValueError, match="Not found"):
                run.finalize(p, d)
        # The restart sees the task as undone, discards the stale commit and redoes it.
        with pytest.raises(ValueError, match="Incomplete target coverage"):
            run.finalize(p, d)
        assert not (Path(d["stage"]) / "features.lance/_versions").exists()
        redone = run.run_task(("example", d["tasks"][0]))
        assert redone["files"] != checkpoints[0]["files"]
        manifest = run.finalize(p, d)
    ds = lance.dataset(Path(d["final"]) / "features.lance", version=manifest["lance_version"])
    assert ds.to_table(columns=["text"])["text"].to_pylist() == ["0", "2", "4", "6", "8"]
    assert "lost_checkpoint_file" in (Path(d["state"]) / "lost_files.jsonl").read_text()


def test_lost_files_beyond_sporadic_loss_are_refused(tmp_path, monkeypatch):
    _, p, d = fixture(tmp_path)
    with ThreadPoolExecutor(2) as decoders:
        setup_worker(monkeypatch, p, d, decoders)
        checkpoints = [run.run_task(("example", task)) for task in d["tasks"]]
    for c in checkpoints:
        (Path(d["stage"]) / "features.lance/data" / c["files"][0]["name"]).unlink()
    # Without the stage's ownership marker a missing file is never treated as a lost rename.
    with pytest.raises(ValueError, match="outside an owned stage"):
        run.verify_all(d)
    (Path(d["stage"]) / "execution.json").write_text("{}")
    # A small dataset tolerates max(8, 1% of tasks) lost files; more means a missing mount.
    monkeypatch.setattr(feature_runtime, "_LOST", {})
    for i in range(9):
        with pytest.raises(ValueError, match="Too many") if i == 8 else nullcontext():
            feature_runtime.lost_file(d, {"id": f"lost-{i}"}, "missing")
