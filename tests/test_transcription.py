import io
import json

import lance
import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf

from tts_data_pipeline.annotations.qwen_asr import client, run
from tts_data_pipeline.feature_runtime import ordered_hash
from tts_data_pipeline.schema import digest


def test_streamed_identity_plan_preserves_hash_and_boundaries():
    ids = [digest(i) for i in range(11)]
    batches = [
        pa.record_batch([pa.array(ids[a:b])], names=["sample_id"])
        for a, b in [(0, 2), (2, 9), (9, 11)]
    ]
    tasks, rows, checksum = run.plan_ids(iter(batches), "test", 4)
    assert rows == 11 and checksum == ordered_hash(ids)
    assert [(t["offset"], t["rows"]) for t in tasks] == [(0, 4), (4, 4), (8, 3)]
    assert [t["ids_sha256"] for t in tasks] == [ordered_hash(ids[i : i + 4]) for i in [0, 4, 8]]
    with pytest.raises(ValueError, match="Null base"):
        run.plan_ids(
            iter([pa.record_batch([pa.array([None], type=pa.string())], names=["sample_id"])]),
            "test",
            4,
        )


def test_publication_identity_counts_across_scanner_batches(tmp_path):
    from collections import Counter

    from tts_data_pipeline.annotations.qwen_asr.schema import annotation_schema

    count = 65539
    ids = [digest(i) for i in range(count)]
    table = pa.Table.from_pylist(
        [
            dict(
                sample_id=sid,
                input_fingerprint="fp",
                status="failed",
                error_code="test",
                result=None,
            )
            for sid in ids
        ],
        schema=annotation_schema(),
    )
    source = dict(
        dataset_id="test", release_id="v0.1", rows=count, ordered_ids_sha256=ordered_hash(ids)
    )
    output = run.publication.publish_output(
        tmp_path / "root",
        source,
        "test-run",
        tmp_path / "work",
        lambda: iter(table.to_batches(max_chunksize=4096)),
        Counter(failed=count),
    )
    assert output["table"]["rows"] == count
    assert output["validation"]["readback_rows"] == count
    assert output["validation"]["unique_targets"] == count


def test_long_stream_keeps_every_segment_and_detects_incomplete_transport():
    events = [
        {"choices": [{"delta": {"content": "language English<asr_text>First part."}}]},
        {
            "choices": [
                {
                    "delta": {"content": "language English<asr_text>Last part."},
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    stream = [b"data: " + json.dumps(e).encode() + b"\n" for e in events]
    with pytest.raises(RuntimeError, match="before final"):
        client.read_stream(stream)
    response = client.read_stream(stream + [b"data: [DONE]\n"])
    parsed = client.parse_content(response["content"])
    assert parsed["text"] == "First part.\nLast part."
    assert parsed["language"] == "English"
    assert client.parse_content("language None<asr_text>")["text"] == ""
    assert client.parse_content("볼래", "ko")["language_source"] == "requested"


def example_row(seconds=1):
    buf = io.BytesIO()
    sf.write(buf, np.ones(16000 * seconds, dtype=np.float32) * 0.1, 16000, format="WAV")
    data = buf.getvalue()
    return dict(
        sample_id=digest("sample"),
        audio={"bytes": data},
        audio_sha256=run.hashlib.sha256(data).hexdigest(),
        sample_rate=16000,
        channels=1,
        language="ko",
        text="Original caption",
    )


def test_complete_audio_and_language_candidate_do_not_replace_primary(monkeypatch):
    calls = []

    def transcribe(endpoint, model, encoded, language=None):
        calls.append((sf.info(io.BytesIO(encoded)).frames, language))
        return dict(
            language="English" if language is None else "Korean",
            text="Bolle" if language is None else "볼래",
            error_code=None,
            raw_response={"content": "raw", "finish_reasons": ["stop"]},
        )

    monkeypatch.setattr(client, "transcribe", transcribe)
    row = example_row(45)
    target, result = run.process(row, dict(endpoint="local", model="model", profile_id="profile"))
    assert target["status"] == "ok" and target["item_count"] == 1
    assert calls == [(45 * 16000, None), (45 * 16000, "ko")]
    assert result["text"] == "Bolle" and result["language_candidate_text"] == "볼래"
    assert result["end_frame"] == 45 * 16000 and result["language_mismatch"]
    assert result["source_text_sha256"] == digest(row["text"])


def test_source_conflict_stops_and_decode_failure_is_recorded():
    plan = dict(endpoint="local", model="model", profile_id="profile")
    row = example_row()
    with pytest.raises(RuntimeError, match="hash conflict"):
        run.process({**row, "audio_sha256": "bad"}, plan)
    data = b"not an audio container"
    target, result = run.process(
        {**row, "audio": {"bytes": data}, "audio_sha256": run.hashlib.sha256(data).hexdigest()},
        plan,
    )
    assert target["status"] == "failed" and target["item_count"] == 0 and result is None


@pytest.mark.parametrize(
    "code,name",
    [
        ("de", "German"),
        ("fr", "French"),
        ("nl", "Dutch"),
        ("pl", "Polish"),
        ("pt", "Portuguese"),
        ("es", "Spanish"),
        ("it", "Italian"),
    ],
)
def test_yodas_language_candidate_routes(code, name, monkeypatch):
    calls = []

    def transcribe(endpoint, model, encoded, language=None):
        calls.append(language)
        return dict(
            language=name if language else "English",
            text="candidate" if language else "main",
            error_code=None,
            raw_response={},
        )

    monkeypatch.setattr(client, "transcribe", transcribe)
    source = dict(example_row(), language=code)
    target, result = run.process(source, dict(endpoint="local", model="test", profile_id="profile"))
    assert target["status"] == "ok" and result["language_mismatch"]
    assert calls == [None, code]
    assert result["text"] == "main" and result["language_candidate_text"] == "candidate"
    assert client.parse_content("candidate", code)["language"] == name


@pytest.mark.parametrize("failed", [False, True])
def test_checkpoint_integrity_and_independent_publication(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(
        client,
        "transcribe",
        lambda *args: dict(language="Korean", text="안녕", error_code=None, raw_response={}),
    )
    profile = dict(test=True)
    row = example_row()
    output = run.process(row, dict(endpoint="local", model="test", profile_id=digest(profile)))
    if failed:
        output = (
            dict(output[0], status="failed", error_code="audio_decode_failed", item_count=0),
            None,
        )
    task = dict(dataset_id="game", offset=0, rows=1, ids_sha256=ordered_hash([row["sample_id"]]))
    work = tmp_path / "work"
    checkpoint = run.save_checkpoint(work, task, "plan", [output])
    assert run.load_checkpoint(work, task, "plan") == checkpoint
    with pytest.raises(ValueError, match="input changed"):
        run.load_checkpoint(work, task, "other_plan")
    run.write_json(work / "execution.json", dict(workers=1))
    plan = dict(
        root=str(tmp_path / "unified"),
        run_id="tts-ann-transcription-test",
        profile=profile,
        profile_id=digest(profile),
        tasks=[task],
        inputs=[
            dict(
                dataset_id="game", release_id="v0.1", rows=1, ordered_ids_sha256=task["ids_sha256"]
            )
        ],
    )
    if failed:
        original_write = run.publication.write_json

        def interrupt_publication(path, value):
            if path.name == "game.publication.json":
                raise RuntimeError("publication interruption")
            original_write(path, value)

        monkeypatch.setattr(run.publication, "write_json", interrupt_publication)
        with pytest.raises(RuntimeError, match="publication interruption"):
            run.publish(plan, work, [checkpoint])
        monkeypatch.setattr(run.publication, "write_json", original_write)
    path = run.publish(plan, work, [checkpoint])
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "complete"
    output = manifest["outputs"][0]
    assert output["coverage"]["missing"] == 0
    assert output["coverage"]["failed"] == int(failed)
    assert manifest["schema_version"] == "qwen-asr-transcription-v2"
    assert output["storage_kind"] == "sample_table" and "tables" not in output
    ref = output["table"]
    ds = lance.dataset(tmp_path / "unified" / ref["table_path"], version=ref["lance_version"])
    restored = ds.to_table().to_pylist()
    assert len(restored) == 1 and restored[0]["sample_id"] == row["sample_id"]
    assert (restored[0]["result"] is None) == failed
    assert restored[0]["status"] == ("failed" if failed else "ok")
    assert not (tmp_path / "unified" / ref["table_path"]).with_name("targets.lance").exists()
    part = run.checkpoint_path(work, task) / "targets.parquet"
    part.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="file changed"):
        run.load_checkpoint(work, task, "plan")


def test_single_table_migration_preserves_empty_text_candidates_and_failures(tmp_path):
    import pyarrow as pa

    from tts_data_pipeline.annotations.qwen_asr import migrate, publication
    from tts_data_pipeline.annotations.qwen_asr.schema import result_schema
    from tts_data_pipeline.contract import annotation_targets_schema, schema_description

    root = tmp_path / "unified"
    ids = [digest(i) for i in range(3)]
    targets = pa.Table.from_pylist(
        [
            dict(
                target_kind="sample",
                target_id=sample_id,
                input_fingerprint=digest([i]),
                status="failed" if i == 1 else "ok",
                error_code="decode_failed" if i == 1 else None,
                item_count=0 if i == 1 else 1,
            )
            for i, sample_id in enumerate(ids)
        ],
        schema=annotation_targets_schema(),
    )
    payloads = [
        dict(
            target_kind="sample",
            target_id=ids[i],
            input_fingerprint=digest([i]),
            item_id=0,
            text="" if i == 0 else "original primary",
            language_candidate_text=None if i == 0 else "different candidate",
            language_candidate_response_json=None if i == 0 else '{"error_code":null}',
        )
        for i in (0, 2)
    ]
    results = pa.Table.from_pylist(payloads, schema=result_schema())
    combined = publication.combine(targets, results).to_pylist()
    assert combined[0]["result"]["text"] == ""  # Empty success is distinct from failure.
    assert combined[1]["result"] is None
    assert combined[2]["result"]["language_candidate_text"] == "different candidate"
    wrong = results.to_pylist()
    wrong[1]["input_fingerprint"] = "wrong"
    with pytest.raises(ValueError, match="fingerprint"):
        publication.combine(targets, pa.Table.from_pylist(wrong, schema=result_schema()))
    tables = {}
    for kind, table in (("targets", targets), ("results", results)):
        path = root / "datasets/game/v0.1/annotations/transcription/old" / (kind + ".lance")
        ds = lance.write_dataset(table, path)
        tables[kind] = dict(
            table_path=str(path.relative_to(root)),
            lance_version=ds.version,
            rows=len(table),
            schema_sha256=digest(schema_description(ds.schema)),
        )
    source = dict(
        alias="game",
        dataset_id="game",
        release_id="v0.1",
        rows=3,
        ordered_ids_sha256=ordered_hash(ids),
    )
    old = root / "annotations/transcription/old/manifest.json"
    run.write_json(
        old,
        dict(
            status="complete",
            task="transcription",
            run_id="old",
            schema_version="qwen-asr-transcription-v1",
            profile={},
            profile_id=digest({}),
            inputs=[source],
            outputs=[
                dict(
                    base_input_alias="game",
                    dataset_id="game",
                    release_id="v0.1",
                    storage_kind="result_table",
                    tables=tables,
                    coverage=dict(ok=2, failed=1, unsupported=0, skipped=0),
                )
            ],
        ),
    )
    original_bytes = old.read_bytes()
    work = tmp_path / "migration"
    path = migrate.migrate_tables(root, old, work)
    manifest = json.loads(path.read_text())
    assert manifest["layout_migration"]["inference_reused"]
    assert manifest["run_id"] != "old"
    ref = manifest["outputs"][0]["table"]
    new = lance.dataset(root / ref["table_path"], version=ref["lance_version"])
    assert new.to_table().to_pylist() == combined
    assert old.read_bytes() == original_bytes
    assert migrate.migrate_tables(root, old, work) == path
    for kind, original in (("targets", targets), ("results", results)):
        ds = lance.dataset(root / tables[kind]["table_path"], version=tables[kind]["lance_version"])
        assert ds.to_table().equals(original)


def test_publication_handles_sliced_nullable_results(tmp_path):
    from collections import Counter

    import pyarrow as pa

    from tts_data_pipeline.annotations.qwen_asr import publication
    from tts_data_pipeline.annotations.qwen_asr.schema import annotation_schema

    # Non-byte-aligned slices reproduce Lance 12's nullable-struct bitmap failure.
    ids = [digest(i) for i in range(100)]
    rows = [
        dict(
            sample_id=sample_id,
            input_fingerprint=digest([i]),
            status="failed" if i % 5 == 0 else "ok",
            error_code="decode_failed" if i % 5 == 0 else None,
            result=None if i % 5 == 0 else dict(text="hello", language_candidate_text="candidate"),
        )
        for i, sample_id in enumerate(ids)
    ]
    table = pa.Table.from_pylist(rows, schema=annotation_schema())
    source = dict(
        dataset_id="game", release_id="v0.1", rows=100, ordered_ids_sha256=ordered_hash(ids)
    )
    output = publication.publish_output(
        tmp_path,
        source,
        "test-sliced",
        tmp_path / "work",
        lambda: table.to_batches(max_chunksize=37),
        Counter(r["status"] for r in rows),
    )
    ref = output["table"]
    ds = lance.dataset(tmp_path / ref["table_path"], version=ref["lance_version"])
    assert ds.to_table().equals(table)


def test_transport_retries_transient_but_not_request_rejection(monkeypatch):
    import urllib.error

    from tts_data_pipeline.annotations.qwen_asr import transport

    sleeps, calls = [], []
    monkeypatch.setattr(transport.time, "sleep", sleeps.append)

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("connection timeout")
        return "ok"

    assert transport.call(flaky) == "ok"
    assert sleeps == [2, 5]

    def rejected():
        raise urllib.error.HTTPError("local", 400, "bad", {}, io.BytesIO(b"invalid model"))

    with pytest.raises(transport.RequestRejected, match="invalid model"):
        transport.call(rejected)
    assert sleeps == [2, 5]

    def unavailable():
        raise urllib.error.HTTPError("local", 503, "busy", {}, io.BytesIO(b"unavailable"))

    with pytest.raises(transport.TransientASRError, match="503"):
        transport.call(unavailable)
    assert sleeps == [2, 5, 2, 5]


@pytest.mark.parametrize("native_frames,rate", [(71, 16000), (439, 44100)])
def test_tiny_zero_rejection_recorded_without_suppressing_other_400(
    tmp_path, monkeypatch, native_frames, rate
):
    from tts_data_pipeline.annotations.qwen_asr import journal, transport

    row = example_row()
    buf = io.BytesIO()
    sf.write(buf, np.zeros(native_frames, dtype=np.float32), rate, format="WAV", subtype="FLOAT")
    data = buf.getvalue()
    row.update(
        audio={"bytes": data}, audio_sha256=run.hashlib.sha256(data).hexdigest(), sample_rate=rate
    )

    def reject(*args):
        raise transport.RequestRejected(400, "Failed to apply Qwen3ASRProcessor")

    monkeypatch.setattr(client, "transcribe", reject)
    plan = dict(
        endpoint="local",
        model="model",
        profile_id="profile",
        _failure_journal=journal.FailureJournal(tmp_path / "failures.jsonl"),
    )
    target, result = run.process(row, plan)
    assert target["error_code"] == "asr_audio_preprocessing_rejected" and result is None
    evidence = json.loads((tmp_path / "failures.jsonl").read_text())
    assert evidence["target_id"] == row["sample_id"] and evidence["details"]["http_status"] == 400
    with pytest.raises(transport.RequestRejected):
        run.process(example_row(), plan)

    # Exactly 10 ms is outside the verified class; unrelated rejections still stop.
    boundary = io.BytesIO()
    sf.write(boundary, np.zeros(160, dtype=np.float32), 16000, format="WAV", subtype="FLOAT")
    boundary_data = boundary.getvalue()
    boundary_row = dict(
        row,
        audio={"bytes": boundary_data},
        audio_sha256=run.hashlib.sha256(boundary_data).hexdigest(),
        sample_rate=16000,
    )
    with pytest.raises(transport.RequestRejected):
        run.process(boundary_row, plan)

    def wrong_configuration(*args):
        raise transport.RequestRejected(400, "Wrong generation configuration")

    monkeypatch.setattr(client, "transcribe", wrong_configuration)
    with pytest.raises(transport.RequestRejected):
        run.process(row, plan)


def test_auto_resume_and_permanent_errors(tmp_path, monkeypatch):
    from tts_data_pipeline.annotations.qwen_asr import transport

    calls, sleeps = [], []

    def temporary_failure(work, workers):
        calls.append(work)
        if len(calls) == 1:
            run.write_json(work / "status.json", dict(verified_rows=512, counts={"ok": 512}))
            raise transport.TransientASRError("endpoint offline")
        return "complete"

    monkeypatch.setattr(run, "_run", temporary_failure)
    monkeypatch.setattr(run.time, "sleep", sleeps.append)
    assert run.run(tmp_path, 64, auto_resume=True) == "complete"
    assert len(calls) == 2 and sleeps == [60]
    assert json.loads((tmp_path / "recovery-events.jsonl").read_text())["recovery_count"] == 1

    def permanent_failure(work, workers):
        raise ValueError("Base manifest changed")

    monkeypatch.setattr(run, "_run", permanent_failure)
    with pytest.raises(ValueError, match="manifest changed"):
        run.run(tmp_path, 64, auto_resume=True)
    assert sleeps == [60]


def test_truncation_evidence_is_written_before_checkpoint(tmp_path, monkeypatch):
    from tts_data_pipeline.annotations.qwen_asr import journal

    log = journal.FailureJournal(tmp_path / "failures.jsonl")
    plan = dict(endpoint="local", model="model", profile_id="profile", _failure_journal=log)
    monkeypatch.setattr(
        client,
        "transcribe",
        lambda *a: dict(
            error_code="asr_output_truncated",
            raw_response=dict(content="unfinished", finish_reasons=["length"]),
        ),
    )
    target, result = run.process(example_row(), plan)
    assert target["status"] == "failed" and result is None
    log.record(target, target["error_code"], from_checkpoint=True)
    lines = (tmp_path / "failures.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["details"]["content"] == "unfinished"
