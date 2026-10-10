import hashlib
import io
import json

import lance
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf
import torch

from tts_data_pipeline.annotations.firered_lid.model import (
    Encoder,
    Frontend,
    infer_records,
    intervals,
    language,
)
from tts_data_pipeline.annotations.firered_lid.publication import load_checkpoint, publish
from tts_data_pipeline.annotations.firered_lid.schema import annotation_schema
from tts_data_pipeline.feature_runtime import file_hash, write_json


def row(values, rate=1000):
    buf = io.BytesIO()
    sf.write(buf, values, rate, format="WAV", subtype="FLOAT")
    data = buf.getvalue()
    return dict(
        sample_id="sample",
        audio={"bytes": data},
        audio_sha256=hashlib.sha256(data).hexdigest(),
        language="en-US",
        num_frames=123,
    )


def frontend_stub():
    frontend = object.__new__(Frontend)
    frontend.features = lambda wave, rate: (torch.from_numpy(wave[:, None].copy()), len(wave))
    return frontend


def test_real_decoded_interval_cap_and_stereo_mean():
    frontend = frontend_stub()
    values = np.column_stack([np.ones(201001), np.zeros(201001)])
    record, features = frontend.prepare(row(values), "profile")
    result = record["result"]
    assert result["decoded_num_samples"] == 201001
    assert result["declared_num_samples"] == 123
    assert result["start_sample"] == 0 and result["end_sample"] == 200000
    assert result["analysis_truncated"] is True
    assert len(features) == 1 and len(features[0]) == 200000
    assert torch.all(features[0] == 0.5)
    assert intervals(31 * 44100, 44100) == [(0, 31 * 44100)]
    assert intervals(200 * 44100, 44100) == [(0, 200 * 44100)]


def test_input_dependency_and_bad_audio():
    frontend = frontend_stub()
    base = row(np.zeros(100))
    record, _ = frontend.prepare(base, "profile")
    unchanged, _ = frontend.prepare(dict(base, text="irrelevant", num_frames=999), "profile")
    assert record["input_fingerprint"] == unchanged["input_fingerprint"]
    changed, _ = frontend.prepare(dict(base, language="ja"), "profile")
    assert record["input_fingerprint"] != changed["input_fingerprint"]
    bad = b"not audio"
    failed, features = frontend.prepare(
        dict(base, audio={"bytes": bad}, audio_sha256=hashlib.sha256(bad).hexdigest()), "profile"
    )
    assert failed["status"] == "failed" and failed["result"] is None and features == []
    nonfinite, _ = frontend.prepare(row(np.array([0, np.nan])), "profile")
    assert nonfinite["error_code"] == "audio_nonfinite"
    with pytest.raises(RuntimeError, match="hash conflict"):
        frontend.prepare(dict(base, audio_sha256="incorrect"), "profile")


def test_language_mapping_and_invalid_tokens():
    assert language("zh mandarin") == "zh"
    assert language("zh yue") == "yue"
    assert language("ja") == "ja"
    assert language("other") == "und"
    assert language("iw") == "he"
    with pytest.raises(ValueError):
        language("<unk>")


def test_bucket_order_and_oom_retry_preserve_identity():
    class Fake(Encoder):
        def __init__(self):
            self.batch_lengths = []

        def predict(self, features):
            self.batch_lengths.append(len(features))
            if len(features) > 2:
                raise torch.cuda.OutOfMemoryError("test batch pressure")
            return [
                dict(raw_language="en", language="en", confidence=float(f[0, 0])) for f in features
            ]

    model = Fake()
    prepared = [
        (
            dict(sample_id=str(i), result=dict(source_language="en-US")),
            [torch.full((length, 1), i / 10)],
        )
        for i, length in enumerate([8, 2, 7, 1])
    ]
    output = infer_records(model, prepared, batch_size=8)
    assert [v["sample_id"] for v in output] == ["0", "1", "2", "3"]
    assert [v["result"]["confidence"] for v in output] == pytest.approx([0, 0.1, 0.2, 0.3])
    assert all(not v["result"]["source_language_mismatch"] for v in output)
    assert model.batch_lengths == [4, 2, 2]


def test_single_invalid_model_output_does_not_fail_other_samples():
    from types import SimpleNamespace

    encoder = object.__new__(Encoder)
    encoder.device = "cpu"
    encoder.tokenizer = SimpleNamespace(detokenize=lambda ids: "en")
    encoder.model = SimpleNamespace(
        process=lambda *args: [
            [dict(yseq=torch.tensor([5]), confidence=torch.tensor(float("nan")))],
            [dict(yseq=torch.tensor([5]), confidence=torch.tensor(0.9))],
        ]
    )
    prepared = [
        (
            dict(sample_id=str(i), status="ok", error_code=None, result=dict(source_language="en")),
            [torch.ones(8, 80)],
        )
        for i in range(2)
    ]
    output = infer_records(encoder, prepared)
    assert output[0]["status"] == "failed" and output[0]["result"] is None
    assert output[0]["error_code"] == "model_invalid_confidence"
    assert output[1]["status"] == "ok" and output[1]["result"]["language"] == "en"


def test_publication_preserves_success_failure_and_checkpoint_integrity(tmp_path):
    work, root = tmp_path / "work", tmp_path / "unified"
    task = dict(id="task", source=0, rows=2)
    record, _ = frontend_stub().prepare(row(np.zeros(100)), "profile")
    record["result"].update(
        raw_language="en", language="en", confidence=0.25, source_language_mismatch=False
    )
    failure = dict(
        sample_id="bad",
        input_fingerprint="fp",
        status="failed",
        error_code="audio_decode_failed",
        result=None,
    )
    table = pa.Table.from_pylist([record, failure], schema=annotation_schema())
    folder = work / "checkpoints/task"
    folder.mkdir(parents=True)
    pq.write_table(table, folder / "results.parquet")
    checkpoint = dict(
        task=task,
        plan_sha256="plan",
        sha256=file_hash(folder / "results.parquet"),
        counts=dict(ok=1, failed=1),
        errors=dict(audio_decode_failed=1),
    )
    write_json(folder / "checkpoint.json", checkpoint)
    assert load_checkpoint(work, task, "plan") == checkpoint
    with pytest.raises(ValueError, match="identity changed"):
        load_checkpoint(work, task, "wrong")
    plan = dict(
        root=str(root),
        output_root=str(root),
        run_id="test",
        profile={},
        profile_id="p",
        scope="test",
        tasks=[task],
        inputs=[dict(dataset_id="test", release_id="v0.1", alias="test/v0.1", target_rows=2)],
    )
    write_json(work / "execution.json", {})
    path = publish(plan, work, {"task": checkpoint})
    manifest = json.loads(path.read_text())
    output = manifest["outputs"][0]
    ds = lance.dataset(
        root / output["table"]["table_path"], version=output["table"]["lance_version"]
    )
    assert ds.to_table().equals(table)
    assert output["coverage"] == dict(
        total_targets=2, missing=0, ok=1, failed=1, unsupported=0, skipped=0
    )
    assert not list(root.rglob("targets.lance"))
    path.unlink()  # Simulate interruption after output publication, before global manifest.
    assert publish(plan, work, {"task": checkpoint}) == path
    (folder / "results.parquet").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="content changed"):
        load_checkpoint(work, task, "plan")
