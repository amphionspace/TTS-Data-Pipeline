import json
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


def tables():
    codec, speaker, text = [], [], []
    for i in range(4):
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


def fixture(tmp_path):
    c, s, t = tables()
    plans = {}
    selection = tmp_path / "selection.json"
    write_json(selection, dict(status="complete"))
    task = dict(
        dataset="example",
        fragment=0,
        offset=0,
        rows=4,
        ordered_ids_sha256=ordered_hash(t["target_id"].to_pylist()),
        id="task",
    )
    source = dict(dataset_id="example", release_id="v0.1", selected_rows=4)
    for kind, table in [("codec", c), ("speaker", s.take([3, 2, 1, 0])), ("text", t)]:
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
            rows=4,
            lance_version=ds.version,
            coverage=dict(total_targets=4, ok=4, failed=0, unsupported=0, skipped=0, missing=0),
            selection=dict(
                mode="selection_branch",
                available_target_rows=4,
                target_count=4,
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
                    target_count=4,
                    target_set_sha256="f" * 64,
                )
            ],
        )
        plans[kind] = tmp_path / f"{kind}.json"
        write_json(plans[kind], p)
    return merge.prepare(
        plans["codec"], plans["speaker"], plans["text"], tmp_path / "work", batch_rows=2
    )


def test_resume_publication_and_sources_preserved(tmp_path, monkeypatch):
    p = fixture(tmp_path)
    d = p["datasets"][0]
    hashes = {x["manifest_path"]: file_hash(x["manifest_path"]) for x in d["sources"].values()}
    original = merge.write_chunk
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated crash")
        return original(*args)

    with monkeypatch.context() as m:
        m.setattr(merge, "write_chunk", fail_second)
        with pytest.raises(RuntimeError, match="simulated"):
            merge.run(p["work"], 1)
    merge.run(p["work"], 1)
    manifest = json.loads((Path(d["final"]) / "manifest.json").read_text())
    assert manifest["validation"]["source_tables_preserved"]
    ds = lance.dataset(Path(d["final"]) / "features.lance", version=manifest["lance_version"])
    out = ds.to_table()
    c, s, t = tables()
    assert out["speaker_embedding"].equals(s["embedding"])
    assert out["codec_codes"].equals(c["codes"])
    assert out.select(t.schema.names).equals(t)
    assert hashes == {path: file_hash(path) for path in hashes}
    merge.run(p["work"], 1)
    assert lance.dataset(Path(d["final"]) / "features.lance").version == ds.version


def test_checkpoint_corruption_is_not_reused(tmp_path):
    p = fixture(tmp_path)
    d = p["datasets"][0]
    stage = Path(d["stage"])
    stage.mkdir(parents=True)
    write_json(stage / "execution.json", dict(owner=digest(p)))
    c, s, t = tables()
    table, differences = merge.merge_tables(
        c.slice(0, 2), s.slice(0, 2), t.slice(0, 2), "example", "v0.1"
    )
    checkpoint = merge.write_chunk(d, d["tasks"][0], table, differences)
    file = stage / "features.lance/data" / checkpoint["files"][0]["name"]
    with file.open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="corrupted"):
        merge.run(p["work"], 1)
