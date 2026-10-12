import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline import merge_features as merge
from tts_data_pipeline.contract import (
    codec_schema,
    codec_text_schema,
    speaker_embedding_schema,
    text_feature_schema,
)
from tts_data_pipeline.feature_runtime import file_hash, ordered_hash, write_json
from tts_data_pipeline.schema import digest


def tables(n=4):
    codec, speaker, text = [], [], []
    for i in range(n):
        sid = digest(i)
        meta = dict(
            dataset_id="example",
            text=f"line {i}",
            language="en",
            text_kind="transcript",
            text_revision=digest([i, "text"]),
            source_key=str(i),
            speaker_id=None,
            speaker_scope=None,
            text_source=0,
        )
        base = dict(
            target_id=sid,
            parent_sample_id=sid,
            target_kind="sample",
            audio_sha256=digest([i, "audio"]),
            start_frame=0,
            end_frame=24000,
            native_sample_rate=24000,
            encoder_input_num_frames=24000,
            profile_id="a" * 64,
            feature_key=digest([i, "key"]),
            input_fingerprint=digest([i, "input"]),
            timeline_profile_id="b" * 64,
            encoder_input_sha256="c" * 64,
            status="ok",
            error_code=None,
        )
        codec.append(
            dict(
                base,
                codes=[[i] * 16] * 13,
                codes_sha256="d" * 64,
                num_codec_frames=13,
                num_codebooks=16,
                **meta,
            )
        )
        speaker.append(
            dict(base, embedding=[float(i)] * 3, embedding_dim=3, embedding_sha256="e" * 64)
        )
        text.append(
            dict(target_id=sid, audio_sha256=base["audio_sha256"], release_id="v0.1", **meta)
        )
    return (
        pa.Table.from_pylist(codec, schema=pa.schema([*codec_schema(16), *codec_text_schema()])),
        pa.Table.from_pylist(speaker, schema=speaker_embedding_schema(3)),
        pa.Table.from_pylist(text, schema=text_feature_schema()),
    )


def change(table, column, index, value):
    values = table[column].to_pylist()
    values[index] = value
    field = table.schema.field(column)
    return table.set_column(
        table.schema.get_field_index(column), field, pa.array(values, type=field.type)
    )


def test_id_join_preserves_payloads_and_text():
    c, s, t = tables()
    out, differences = merge.merge_tables(
        c.take([3, 1, 0, 2]), s.take([2, 0, 3, 1]), t, "example", "v0.1"
    )
    assert not differences
    assert out["codec_codes"].equals(c["codes"])
    assert out["speaker_embedding"].equals(s["embedding"])
    assert out.select(t.schema.names).equals(t)


@pytest.mark.parametrize(
    "kind,column,value",
    [
        (0, "audio_sha256", "0" * 64),
        (0, "text", "different"),
        (1, "status", "failed"),
        (1, "native_sample_rate", 16000),
        (1, "parent_sample_id", "0" * 64),
        (0, "num_codec_frames", 12),
        (0, "encoder_input_num_frames", 23999),
        (1, "embedding", [float("nan")] * 3),
    ],
)
def test_reject_bad_matches(kind, column, value):
    items = list(tables())
    items[kind] = change(items[kind], column, 0, value)
    with pytest.raises(ValueError):
        merge.merge_tables(*items, "example", "v0.1")


def test_duplicate_and_missing_targets_rejected():
    c, s, t = tables()
    for bad in [s.slice(0, 3), s.take([0, 0, 2, 3])]:
        with pytest.raises(ValueError):
            merge.merge_tables(c, bad, t, "example", "v0.1")


def test_small_native_frame_differences_retained():
    c, s, t = tables()
    for delta in [1, 2, 156, 312, 480]:
        changed = change(
            change(s, "end_frame", 0, 24000 + delta), "encoder_input_num_frames", 0, 24000 + delta
        )
        out, diff = merge.merge_tables(c, changed, t, "example", "v0.1")
        assert len(diff) == 1
        assert out["speaker_end_frame"][0].as_py() == 24000 + delta
    with pytest.raises(ValueError, match="more than 20 ms"):
        merge.merge_tables(c, change(s, "end_frame", 0, 24481), t, "example", "v0.1")


class InProcessPool(ThreadPoolExecutor):
    """Stands in for the spawn process pools so tests can inject faults in-process."""

    def __init__(self, max_workers, mp_context=None, **kwargs):
        super().__init__(max_workers, **kwargs)


def fixture(tmp_path, *, rows=4, batch_rows=2, work="work", output_root=None):
    c, s, t = tables(rows)
    plans = {}
    selection = tmp_path / "selection.json"
    write_json(selection, dict(status="complete"))
    task = dict(
        dataset="example",
        fragment=0,
        offset=0,
        rows=rows,
        ordered_ids_sha256=ordered_hash(t["target_id"].to_pylist()),
        id="task",
    )
    source = dict(dataset_id="example", release_id="v0.1", selected_rows=rows)
    reversed_speaker = s.take(list(reversed(range(rows))))
    for kind, table in [("codec", c), ("speaker", reversed_speaker), ("text", t)]:
        final = tmp_path / kind
        ds = lance.write_dataset(table, final / "features.lance")
        ds.create_scalar_index("target_id", "BTREE")
        manifest_kind = "speaker_embedding" if kind == "speaker" else kind
        manifest = dict(
            kind=manifest_kind,
            status="complete",
            target_kind="sample",
            dataset_id="example",
            release_id="v0.1",
            profile_id="a" * 64,
            profile_name="test",
            run_id=f"example-{manifest_kind}-test-20261001T010000bjt-01",
            finished_at="2026-10-01T01:00:00+08:00",
            rows=rows,
            lance_version=ds.version,
            coverage=dict(
                total_targets=rows, ok=rows, failed=0, unsupported=0, skipped=0, missing=0
            ),
            selection=dict(
                mode="selection_branch",
                available_target_rows=rows,
                target_count=rows,
                input_alias="samples",
                filter="selection_reason = 0",
                manifest_path="selection.json",
                manifest_sha256=file_hash(selection),
                target_set_sha256="f" * 64,
            ),
            inputs=[dict(alias="samples", branch="selection", lance_version=1)],
        )
        write_json(final / "manifest.json", manifest)
        p = dict(
            root=str(tmp_path),
            selection_path=str(selection),
            selection_sha256=file_hash(selection),
            datasets=[
                dict(
                    source=source,
                    final=str(final),
                    tasks=[task],
                    target_count=rows,
                    target_set_sha256="f" * 64,
                )
            ],
        )
        plans[kind] = tmp_path / f"{kind}.json"
        write_json(plans[kind], p)
    return merge.prepare(
        plans["codec"],
        plans["speaker"],
        plans["text"],
        tmp_path / work,
        batch_rows=batch_rows,
        output_root=output_root,
    )


def fail_source_rows(tmp_path, kind, indices, error):
    """Rewrite a source table so the given rows failed, and fix its manifest coverage."""
    src = tmp_path / kind
    ds = lance.dataset(src / "features.lance")
    rows = ds.to_table().to_pylist()
    for row in rows:
        if row["target_id"] in indices:
            row.update(status="failed", error_code=error)
            if kind == "codec":
                row.update(codes=None, codes_sha256=None, num_codec_frames=None)
            else:
                row.update(embedding=None, embedding_sha256=None)
    ds = lance.write_dataset(
        pa.Table.from_pylist(rows, schema=ds.schema), src / "features.lance", mode="overwrite"
    )
    manifest = json.loads((src / "manifest.json").read_text())
    manifest["lance_version"] = ds.version
    manifest["coverage"].update(ok=len(rows) - len(indices), failed=len(indices))
    write_json(src / "manifest.json", manifest)


def published_table(d):
    manifest = json.loads((Path(d["final"]) / "manifest.json").read_text())
    ds = lance.dataset(Path(d["final"]) / "features.lance", version=manifest["lance_version"])
    return manifest, ds.to_table()


def test_process_pool_matches_sequential_merge(tmp_path):
    p = fixture(tmp_path, rows=7, batch_rows=2)
    d = p["datasets"][0]
    assert len(d["tasks"]) == 4
    merge.run(p["work"], 3)
    manifest, out = published_table(d)
    c, s, t = tables(7)
    expected, _ = merge.merge_tables(c, s, t, "example", "v0.1")
    assert out.equals(expected)
    assert manifest["rows"] == 7 and manifest["selection"]["mode"] == "selection_branch"
    assert manifest["excluded_targets"] == dict(
        rule=merge.EXCLUSION_RULE,
        input_rows=7,
        retained=7,
        total=0,
        by_source=dict(codec={}, speaker={}),
    )
    assert not (Path(d["final"]) / "execution.json").exists()
    assert Path(d["state"]).parent == Path(p["work"]) / "state"

    # Sequential processing of the same plan in one process must give identical output.
    q = merge.prepare(
        tmp_path / "codec.json",
        tmp_path / "speaker.json",
        tmp_path / "text.json",
        tmp_path / "seq-work",
        batch_rows=2,
        output_root=tmp_path / "seq-out",
    )
    e = q["datasets"][0]
    merge.claim_stage(q, e)
    sources = {k: merge.source_table(v) for k, v in e["sources"].items()}
    schema = merge.output_schema(*(sources[k].schema for k in merge.KINDS))
    checkpoints = [merge.merge_chunk(q, e, task, sources, schema) for task in e["tasks"]]
    merge.finalize(q, e, checkpoints, schema)
    sequential, seq_out = published_table(e)
    assert seq_out.equals(out)
    assert sequential["schema_sha256"] == manifest["schema_sha256"]
    assert sequential["validation"] == manifest["validation"]


def test_resume_after_partial_run_reuses_checkpoints(tmp_path):
    p = fixture(tmp_path, rows=6, batch_rows=2)
    d = p["datasets"][0]
    merge.claim_stage(p, d)
    c, s, t = tables(6)
    table, differences = merge.merge_tables(
        c.slice(0, 2), s.slice(0, 2), t.slice(0, 2), "example", "v0.1"
    )
    first = merge.write_chunk(d, d["tasks"][0], table, differences)
    checkpoint = Path(d["state"]) / "checkpoints" / f"{d['tasks'][0]['id']}.json"
    before = checkpoint.stat().st_mtime_ns
    merge.run(p["work"], 2)
    assert checkpoint.stat().st_mtime_ns == before
    data_files = {f.name for f in (Path(d["final"]) / "features.lance/data").iterdir()}
    assert first["files"][0]["name"] in data_files and len(data_files) == 3
    manifest, out = published_table(d)
    expected, _ = merge.merge_tables(c, s, t, "example", "v0.1")
    assert out.equals(expected)
    status = json.loads((Path(p["work"]) / "status.json").read_text())
    assert status["phase"] == "complete" and status["rows"] == 6
    # A published dataset is verified and skipped; the output is left untouched.
    merge.run(p["work"], 2)
    assert lance.dataset(Path(d["final"]) / "features.lance").version == manifest["lance_version"]
    progress = json.loads((Path(p["work"]) / "datasets" / "example.json").read_text())
    assert progress == dict(phase="complete", rows_done=6, rows_total=6)


def test_worker_failure_surfaces_and_rerun_completes(tmp_path, monkeypatch):
    p = fixture(tmp_path, rows=6, batch_rows=2)
    d = p["datasets"][0]
    hashes = {x["manifest_path"]: file_hash(x["manifest_path"]) for x in d["sources"].values()}
    original = merge.run_task
    failed = []

    def fail_once(plan_path, index, task):
        if task["offset"] == 2 and not failed:
            failed.append(task)
            raise RuntimeError("simulated crash")
        return original(plan_path, index, task)

    monkeypatch.setattr(merge, "ProcessPoolExecutor", InProcessPool)
    monkeypatch.setattr(merge, "run_task", fail_once)
    with pytest.raises(RuntimeError, match="simulated"):
        merge.run(p["work"], 2)
    status = json.loads((Path(p["work"]) / "status.json").read_text())
    assert status["phase"] == "failed" and status["error_type"] == "RuntimeError"
    assert not Path(d["final"]).exists()
    merge.run(p["work"], 2)
    manifest, out = published_table(d)
    c, s, t = tables(6)
    assert out["speaker_embedding"].equals(s["embedding"])
    assert out["codec_codes"].equals(c["codes"])
    assert out.select(t.schema.names).equals(t)
    assert manifest["validation"]["source_tables_preserved"]
    assert hashes == {path: file_hash(path) for path in hashes}
    merge.run(p["work"], 2)
    assert lance.dataset(Path(d["final"]) / "features.lance").version == manifest["lance_version"]


def test_codec_failures_are_excluded_and_counted(tmp_path):
    fixture(tmp_path, rows=6, batch_rows=2)
    c, s, t = tables(6)
    ids = t["target_id"].to_pylist()
    fail_source_rows(tmp_path, "codec", {ids[1], ids[4]}, "codec_failure")
    fail_source_rows(tmp_path, "speaker", {ids[4], ids[5]}, "speaker_failure")
    q = merge.prepare(
        tmp_path / "codec.json",
        tmp_path / "speaker.json",
        tmp_path / "text.json",
        tmp_path / "excluded-work",
        batch_rows=2,
        output_root=tmp_path / "excluded-out",
    )
    d = q["datasets"][0]
    assert d["output_count"] == 3 and d["excluded_count"] == 3
    assert d["source_failures"] == dict(codec=2, speaker=2)
    merge.run(q["work"], 2)
    manifest, out = published_table(d)
    kept = [0, 2, 3]
    assert out["target_id"].to_pylist() == [ids[i] for i in kept]
    expected, _ = merge.merge_tables(c.take(kept), s.take(kept), t.take(kept), "example", "v0.1")
    assert out.equals(expected)
    assert manifest["rows"] == 3 and manifest["coverage"]["ok"] == 3
    assert manifest["excluded_targets"] == dict(
        rule=merge.EXCLUSION_RULE,
        input_rows=6,
        retained=3,
        total=3,
        by_source=dict(codec={"codec_failure": 2}, speaker={"speaker_failure": 2}),
    )
    assert manifest["selection"]["mode"] == "subset"
    assert manifest["selection"]["target_count"] == 3
    assert "reference_filtering" not in manifest
    targets = lance.dataset(Path(d["final"]) / "targets.lance")
    assert sorted(targets.to_table()["target_id"].to_pylist()) == sorted(ids[i] for i in kept)
    assert manifest["validation"]["audio_hashes_checked"] == 6


def test_incomplete_text_and_inconsistent_coverage_rejected(tmp_path):
    fixture(tmp_path, rows=4, batch_rows=2)
    ids = tables(4)[2]["target_id"].to_pylist()
    plans = [tmp_path / f"{k}.json" for k in ("codec", "speaker", "text")]
    fail_source_rows(tmp_path, "text", {ids[0]}, "text_failure")
    with pytest.raises(ValueError, match="incomplete"):
        merge.prepare(*plans, tmp_path / "work-text", batch_rows=2)
    manifest_path = tmp_path / "text/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["coverage"].update(ok=4, failed=0)
    write_json(manifest_path, manifest)
    # Codec manifest claims a failure the table does not contain.
    codec_manifest = tmp_path / "codec/manifest.json"
    manifest = json.loads(codec_manifest.read_text())
    manifest["coverage"].update(ok=3, failed=1)
    write_json(codec_manifest, manifest)
    with pytest.raises(ValueError, match="coverage differs"):
        merge.prepare(*plans, tmp_path / "work-codec", batch_rows=2)


def test_invalid_failed_row_rejected_in_chunk(tmp_path):
    p = fixture(tmp_path, rows=4, batch_rows=2)
    d = p["datasets"][0]
    merge.claim_stage(p, d)
    sources = {k: merge.source_table(v) for k, v in d["sources"].items()}
    schema = merge.output_schema(*(sources[k].schema for k in merge.KINDS))
    codec = change(sources["codec"].to_table(), "status", 1, "failed")
    sources["codec"] = lance.write_dataset(codec, tmp_path / "bad-codec.lance")
    with pytest.raises(ValueError, match="Invalid codec status"):
        merge.merge_chunk(p, d, d["tasks"][0], sources, schema)


def test_finalize_waits_for_every_returned_chunk(tmp_path, monkeypatch):
    p = fixture(tmp_path, rows=4, batch_rows=2)
    d = p["datasets"][0]
    original_task, original_finalize = merge.run_task, merge.finalize_dataset
    written, second_returned, release = threading.Event(), threading.Event(), threading.Event()
    finalized = []

    def slow_first(plan_path, index, task):
        result = original_task(plan_path, index, task)
        if task["offset"] == 0:
            # The checkpoint file exists now, but the task has not returned yet.
            written.set()
            assert release.wait(30)
        else:
            second_returned.set()
        return result

    def record(plan_path, index):
        finalized.append(time.monotonic())
        return original_finalize(plan_path, index)

    monkeypatch.setattr(merge, "ProcessPoolExecutor", InProcessPool)
    monkeypatch.setattr(merge, "run_task", slow_first)
    monkeypatch.setattr(merge, "finalize_dataset", record)
    errors = []

    def target():
        try:
            merge.run(p["work"], 2)
        except BaseException as exc:  # pragma: no cover - reported by the assertions below
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    try:
        assert written.wait(30) and second_returned.wait(30)
        time.sleep(1.0)
        checkpoints = Path(d["state"]) / "checkpoints"
        assert {f.stem for f in checkpoints.glob("*.json")} == {t["id"] for t in d["tasks"]}
        assert not finalized and not Path(d["final"]).exists()
        progress = json.loads((Path(p["work"]) / "datasets" / "example.json").read_text())
        assert progress["phase"] == "merging" and progress["rows_done"] == 2
        released_at = time.monotonic()
        release.set()
    finally:
        release.set()
        thread.join(60)
    assert not errors and not thread.is_alive()
    assert len(finalized) == 1 and finalized[0] >= released_at
    assert Path(d["final"]).exists()
