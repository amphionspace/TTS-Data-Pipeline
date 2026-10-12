import json
import re

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.annotations.qwen_asr import metadata_import as imp
from tts_data_pipeline.annotations.qwen_asr.schema import annotation_schema
from tts_data_pipeline.feature_runtime import file_hash, write_json
from tts_data_pipeline.schema import sample_id


@pytest.fixture
def fixture(tmp_path):
    root, metadata, work = (tmp_path / name for name in ("root", "metadata", "work"))
    rows, records = [], []
    entry = dict(
        path="ZH/ZH-B000000.json",
        source_archive="ZH/ZH-B000000.tar",
        source_snapshot="source-file-v1:sha256:fixture",
    )
    for i, text in enumerate([" 正文！ ", "。", "", None]):
        key = f"ZH_B00007_S00000_W{i:06d}"
        rows.append(
            dict(
                sample_id=sample_id("emilia", entry["source_snapshot"], key),
                source_key=key,
                source_snapshot=entry["source_snapshot"],
                audio_sha256=f"audio-{i}",
                text="original",
                language="zh",
                sample_rate=24000,
                source_locator_json=json.dumps(
                    dict(path="Emilia/" + entry["source_archive"], member=key + ".mp3")
                ),
            )
        )
        records.append(
            dict(
                id=key,
                wav=f"folder/mp3/{key}.mp3",
                text=text,
                language="ZH",
                timestamps=[dict(start=-99, end=1234567)],
            )
        )
    write_json(metadata / entry["path"], list(reversed(records)))
    release = root / "datasets/emilia/v0.1"
    ds = lance.write_dataset(
        pa.Table.from_pylist(rows),
        release / "samples.lance",
        max_rows_per_file=2,
        max_rows_per_group=2,
    )
    write_json(
        release / "manifest.json",
        dict(
            status="complete",
            validation=dict(unique_ids=4),
            rows=4,
            inputs=[dict(path=entry["source_archive"], source_snapshot=entry["source_snapshot"])],
            table_path="samples.lance",
            lance_version=ds.version,
        ),
    )
    path = metadata / entry["path"]
    audit = tmp_path / "audit.json"
    write_json(
        audit,
        dict(
            complete=True,
            per_file_results=[
                dict(
                    path=entry["path"],
                    sha256=file_hash(path),
                    bytes=path.stat().st_size,
                    rows=4,
                )
            ],
        ),
    )
    plan = imp.prepare(root, metadata, audit, work)
    return root, metadata, work, plan, rows, records


def test_import_roundtrip_resume_and_nullable_evidence(fixture):
    root, metadata, work, plan, rows, records = fixture
    assert re.fullmatch(r"tts-ann-transcription-\d{8}T\d{6}bjt-\d{2,}", plan["run_id"])
    imp.run(work, workers=1, publish=False)
    before = {
        p.name: (p.stat().st_mtime_ns, file_hash(p))
        for p in (work / "checkpoints").glob("*.parquet")
    }
    path = imp.run(work, workers=2)
    manifest = json.loads(path.read_text())
    output = manifest["outputs"][0]
    table = lance.dataset(root / output["table"]["table_path"]).to_table()
    assert table.schema == annotation_schema()
    result = table.to_pylist()
    assert [r["sample_id"] for r in result] == [r["sample_id"] for r in rows]
    assert [r["result"]["text"] for r in result[:3]] == [r["text"] for r in records[:3]]
    assert result[3]["status"] == "failed"
    assert result[3]["result"] is None
    for row in result[:3]:
        assert row["result"]["language"] == "zh"
        assert row["result"]["start_frame"] is None
        assert row["result"]["end_frame"] is None
        assert row["result"]["raw_response_json"] is None
        assert row["result"]["language_candidate_text"] is None
    after = {
        p.name: (p.stat().st_mtime_ns, file_hash(p))
        for p in (work / "checkpoints").glob("*.parquet")
    }
    assert before == after
    published_hash = file_hash(path)
    assert imp.run(work, workers=1) == path
    assert file_hash(path) == published_hash


def test_identity_conflict_does_not_become_failed_annotation(fixture):
    _, _, _, plan, rows, records = fixture
    entry = plan["inventory"][0]
    bad = dict(records[0], wav="wrong.mp3")
    with pytest.raises(ValueError, match="Source identity conflict"):
        imp.convert_row(rows[0], bad, entry, plan["profile_id"])


def test_source_change_blocks_resume(fixture):
    _, metadata, work, plan, _, _ = fixture
    path = metadata / plan["inventory"][0]["path"]
    path.write_text("[]")
    with pytest.raises(ValueError, match="Metadata changed"):
        imp.run(work)


def test_missing_identity_prevents_checkpoint(fixture):
    _, metadata, work, plan, _, records = fixture
    entry = plan["inventory"][0]
    path = metadata / entry["path"]
    write_json(path, records[1:])
    entry.update(sha256=file_hash(path), rows=3)
    imp._initialize(plan)
    with pytest.raises(ValueError, match="Missing metadata identity"):
        imp._convert_fragment(plan["tasks"][0], work, "test-plan")
    assert not imp._checkpoint(work, plan["tasks"][0]).exists()


@pytest.mark.parametrize("language", [None, {}, "UNKNOWN"])
def test_invalid_language_is_a_failed_row(fixture, language):
    _, _, _, plan, rows, records = fixture
    record = dict(records[0], language=language)
    result = imp.convert_row(rows[0], record, plan["inventory"][0], plan["profile_id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "upstream_language_invalid"
    assert result["result"] is None


def test_duplicate_metadata_and_corrupt_checkpoint_are_rejected(fixture):
    _, metadata, work, plan, _, records = fixture
    entry = dict(plan["inventory"][0])
    path = metadata / entry["path"]
    original = path.read_bytes()
    write_json(path, records + [records[0]])
    entry.update(sha256=file_hash(path), rows=5)
    with pytest.raises(ValueError, match="duplicate metadata identity"):
        imp.load_metadata(plan, entry)
    path.write_bytes(original)
    task = plan["tasks"][0]
    checkpoint = imp._checkpoint(work, task)
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"corrupt")
    write_json(
        checkpoint.with_suffix(".json"),
        dict(
            plan_sha256=file_hash(work / "plan.json"),
            rows=task["rows"],
            sha256="incorrect",
        ),
    )
    with pytest.raises(ValueError, match="Checkpoint integrity"):
        imp.verified_checkpoint(work, task, file_hash(work / "plan.json"))
