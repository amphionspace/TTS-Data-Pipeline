import json
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from test_codec_run import TEXT_SOURCES, fixture

from tts_data_pipeline.codec.qwen3_12hz.text import materialize_text, selected_metadata
from tts_data_pipeline.feature_runtime import ordered_hash
from tts_data_pipeline.schema import digest
from tts_data_pipeline.text.metadata import annotation_revisions

ANNOTATED_SOURCES = {
    **TEXT_SOURCES,
    "1": {"kind": "annotation", "normalization": "python.str.strip"},
}


def test_selected_text_and_language_keep_raw_revision_identity():
    row = dict(
        text=" original ",
        selected_text="original",
        selected_text_source=0,
        language="en-US",
        selected_language="en",
        dataset_id="test",
        source_key="1",
        speaker_id=None,
        speaker_scope=None,
        text_kind="transcript",
    )
    result = selected_metadata(row, TEXT_SOURCES)
    assert result["text"] == "original" and result["language"] == "en"
    assert result["text_revision"] == digest(
        ["selected-text-v1", " original ", TEXT_SOURCES["0"]["normalization"]]
    )
    for changes in [
        dict(selected_text=""),
        dict(selected_text=None),
        dict(selected_text_source=1),
        dict(selected_text_source=None),
    ]:
        with pytest.raises(ValueError):
            selected_metadata({**row, **changes}, TEXT_SOURCES)
    annotation = {**TEXT_SOURCES, "1": {"kind": "annotation", "normalization": "python.str.strip"}}
    with pytest.raises(ValueError, match="pinned raw revision"):
        selected_metadata({**row, "selected_text_source": 1}, annotation)
    with pytest.raises(ValueError, match="normalized annotation revision"):
        selected_metadata({**row, "selected_text_source": 1}, annotation, " other ")
    asr = selected_metadata({**row, "selected_text_source": 1}, annotation, " original\n")
    assert asr["text"] == "original" and asr["text_source"] == 1
    assert asr["text_revision"] == digest(["selected-text-v1", " original\n", "python.str.strip"])
    fallback = selected_metadata(
        {**row, "selected_text": None, "selected_text_source": None, "selected_language": None},
        TEXT_SOURCES,
    )
    assert fallback["text"] == " original " and fallback["language"] == "en-US"


def test_text_materialization_rejects_misaligned_targets(tmp_path):
    rows, p, d = fixture(tmp_path)
    ids = [row["sample_id"] for row in rows[::2]]
    source = lance.dataset(Path(p["root"]) / d["source"]["table_path"]).checkout_version(
        (d["source"]["branch"], d["source"]["lance_version"])
    )
    features = lance.write_dataset(pa.table({"target_id": ids[::-1]}), tmp_path / "wrong.lance")
    with pytest.raises(Exception, match="Codec/text target order differs"):
        materialize_text(features, source, d["tasks"], TEXT_SOURCES)
    features = lance.write_dataset(pa.table({"target_id": ids}), tmp_path / "good.lance")
    result = materialize_text(features, source, d["tasks"], TEXT_SOURCES)
    assert result["rows"] == 5 and result["original_codec_files_preserved"]
    with pytest.raises(ValueError, match="fresh unpublished"):
        materialize_text(features, source, d["tasks"], TEXT_SOURCES)


def open_source(p, d):
    return lance.dataset(Path(p["root"]) / d["source"]["table_path"]).checkout_version(
        (d["source"]["branch"], d["source"]["lance_version"])
    )


def codec_table(path, ids):
    """A fresh codec snapshot with one small fragment per target, like a published run."""
    return lance.write_dataset(pa.table({"target_id": ids}), path, max_rows_per_file=2)


def plan_tasks(source, task_size):
    tasks = []
    for fragment in source.get_fragments():
        ids = (
            source.scanner(
                fragments=[fragment],
                columns=["sample_id"],
                filter="selection_reason = 0",
                scan_in_order=True,
            )
            .to_table()["sample_id"]
            .to_pylist()
        )
        for offset in range(0, len(ids), task_size):
            part = ids[offset : offset + task_size]
            tasks.append(
                dict(
                    fragment=fragment.fragment_id,
                    offset=offset,
                    rows=len(part),
                    ordered_ids_sha256=ordered_hash(part),
                )
            )
    return tasks


def annotated_fixture(tmp_path, mismatch=False):
    """The selection fixture with two targets re-pinned to annotation text source 1."""
    rows, p, d = fixture(tmp_path)
    table = open_source(p, d).to_table()
    ids = table["sample_id"].to_pylist()
    annotated = {ids[2]: " two words \n", ids[6]: "\tsix words"}
    column = lambda name, value: pa.array(  # noqa: E731
        [value(i) for i in ids], type=table.schema.field(name).type
    )
    table = table.set_column(
        table.schema.get_field_index("selected_text"),
        "selected_text",
        column(
            "selected_text",
            lambda i: (
                annotated[i].strip() + ("!" if mismatch else "")
                if i in annotated
                else table["selected_text"][ids.index(i)].as_py()
            ),
        ),
    )
    table = table.set_column(
        table.schema.get_field_index("selected_text_source"),
        "selected_text_source",
        column("selected_text_source", lambda i: 1 if i in annotated else 0),
    )
    source = lance.write_dataset(table, tmp_path / "annotated.lance", max_rows_per_file=3)
    asr = lance.write_dataset(
        pa.table(
            {
                "sample_id": list(annotated),
                "status": ["ok"] * len(annotated),
                "result": pa.array(
                    [{"text": v} for v in annotated.values()], pa.struct([("text", pa.string())])
                ),
            }
        ),
        tmp_path / "asr.lance",
    )
    selection = {
        "text_sources": ANNOTATED_SOURCES,
        "annotation_inputs": [
            {
                "dataset_id": "example",
                "table_path": "asr.lance",
                "lance_version": asr.version,
                "text_source": 1,
            }
        ],
    }
    selection_path = tmp_path / "annotated-selection.json"
    selection_path.write_text(json.dumps(selection))
    targets = source.to_table(columns=["sample_id"], filter="selection_reason = 0")
    return source, plan_tasks(source, 2), targets["sample_id"].to_pylist(), selection_path


def test_parallel_materialization_matches_sequential(tmp_path):
    rows, p, d = fixture(tmp_path)
    source = open_source(p, d)
    ids = [row["sample_id"] for row in rows[::2]]
    sequential = codec_table(tmp_path / "sequential.lance", ids)
    expected = materialize_text(sequential, source, d["tasks"], TEXT_SOURCES)
    parallel = codec_table(tmp_path / "parallel.lance", ids)
    # Tiny chunks force several units per fragment and a bounded look-ahead queue.
    actual = materialize_text(
        parallel, source, d["tasks"], TEXT_SOURCES, workers=2, chunk_rows=2, lookahead=1
    )
    assert actual == expected
    assert actual["rows"] == 5 and len(actual["ordered_text_sha256"]) == 64
    columns = ["target_id", *expected["columns"]]
    assert parallel.to_table(columns=columns).equals(sequential.to_table(columns=columns))
    assert parallel.to_table(columns=["text"])["text"].to_pylist() == ["0", "2", "4", "6", "8"]
    with pytest.raises(ValueError, match="fresh unpublished"):
        materialize_text(parallel, source, d["tasks"], TEXT_SOURCES, workers=2)


def test_parallel_materialization_with_annotation_sources(tmp_path):
    source, tasks, ids, selection_path = annotated_fixture(tmp_path)
    selection = json.loads(selection_path.read_text())
    revisions = annotation_revisions(tmp_path, selection, "example")
    sequential = codec_table(tmp_path / "sequential.lance", ids)
    expected = materialize_text(sequential, source, tasks, ANNOTATED_SOURCES, revisions)
    parallel = codec_table(tmp_path / "parallel.lance", ids)
    actual = materialize_text(
        parallel,
        source,
        tasks,
        ANNOTATED_SOURCES,
        revisions,
        workers=2,
        selection_path=selection_path,
        root=tmp_path,
        dataset_id="example",
        chunk_rows=3,
    )
    assert actual == expected
    columns = ["target_id", *expected["columns"]]
    assert parallel.to_table(columns=columns).equals(sequential.to_table(columns=columns))
    stored = parallel.to_table(columns=["text", "text_source", "text_revision"]).to_pylist()
    assert [r["text_source"] for r in stored] == [0, 1, 0, 1, 0]
    assert stored[1]["text"] == "two words" and stored[3]["text"] == "six words"
    assert stored[1]["text_revision"] == digest(
        ["selected-text-v1", " two words \n", "python.str.strip"]
    )
    # Workers rebuild the annotation lookup themselves, so the closure alone is not enough.
    with pytest.raises(ValueError, match="selection_path"):
        materialize_text(
            codec_table(tmp_path / "closure.lance", ids),
            source,
            tasks,
            ANNOTATED_SOURCES,
            revisions,
            workers=2,
        )
    # Without any revision lookup both paths refuse annotation rows the same way.
    for kwargs in [{}, dict(workers=2)]:
        with pytest.raises(Exception, match="pinned raw revision"):
            materialize_text(
                codec_table(tmp_path / "missing.lance", ids),
                source,
                tasks,
                ANNOTATED_SOURCES,
                **kwargs,
            )
        lance.dataset(tmp_path / "missing.lance").drop(tmp_path / "missing.lance")


def test_parallel_materialization_rejects_bad_targets_and_annotations(tmp_path):
    source, tasks, ids, selection_path = annotated_fixture(tmp_path, mismatch=True)
    pinned = dict(selection_path=selection_path, root=tmp_path, dataset_id="example")
    selection = json.loads(selection_path.read_text())
    revisions = annotation_revisions(tmp_path, selection, "example")
    with pytest.raises(Exception, match="normalized annotation revision"):
        materialize_text(
            codec_table(tmp_path / "mismatch.lance", ids),
            source,
            tasks,
            ANNOTATED_SOURCES,
            revisions,
            workers=2,
            **pinned,
        )
    rows, p, d = fixture(tmp_path / "plain")
    source = open_source(p, d)
    ids = [row["sample_id"] for row in rows[::2]]
    with pytest.raises(Exception, match="Codec/text target order differs"):
        materialize_text(
            codec_table(tmp_path / "wrong.lance", ids[::-1]),
            source,
            d["tasks"],
            TEXT_SOURCES,
            workers=2,
        )
    with pytest.raises(ValueError, match="order differs"):
        materialize_text(
            codec_table(tmp_path / "short.lance", ids[:-1]),
            source,
            d["tasks"],
            TEXT_SOURCES,
            workers=2,
        )
    with pytest.raises(ValueError, match="extra targets"):
        materialize_text(
            codec_table(tmp_path / "extra.lance", ids + ids[:1]),
            source,
            d["tasks"],
            TEXT_SOURCES,
            workers=2,
        )
    with pytest.raises(ValueError, match="at least one worker"):
        materialize_text(
            codec_table(tmp_path / "zero.lance", ids), source, d["tasks"], TEXT_SOURCES, workers=0
        )
