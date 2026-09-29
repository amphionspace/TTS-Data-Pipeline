import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf

from tts_data_pipeline import codec_run as run
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


def fixture(tmp_path):
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
