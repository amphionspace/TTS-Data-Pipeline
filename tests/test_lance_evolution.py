"""Exercise the actual storage operations required by later annotation jobs."""

import hashlib

import lance
import pyarrow as pa

from tts_data_pipeline.writer import write_records


def test_quality_struct_distinguishes_missing_failure_and_zero(tmp_path):
    from test_schema import example

    from tts_data_pipeline.contract import annotation_type
    from tts_data_pipeline.schema import record_revision, sample_id

    rows = []
    for i in range(4):
        row = example()
        row["source_key"] = str(i)
        row["sample_id"] = sample_id(row["dataset_id"], row["source_snapshot"], str(i))
        row["record_revision"] = record_revision(row)
        rows.append(row)
    path = tmp_path / "quality.lance"
    write_records(rows, path)
    ds = lance.dataset(path)
    base_version = ds.version
    original_files = {
        p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in (path / "data").glob("*.lance")
    }
    name = "ann__audio_quality__run_001"
    dtype = annotation_type(pa.struct([pa.field("score", pa.float64())]))
    ds.merge(
        pa.table(
            {
                "sample_id": [rows[i]["sample_id"] for i in [3, 0, 2]],
                name: pa.array(
                    [
                        {
                            "status": "failed",
                            "input_fingerprint": "fixture",
                            "error_code": "decode_failed",
                            "result": None,
                        },
                        {
                            "status": "ok",
                            "input_fingerprint": "fixture",
                            "error_code": None,
                            "result": {"score": 4.1},
                        },
                        {
                            "status": "ok",
                            "input_fingerprint": "fixture",
                            "error_code": None,
                            "result": {"score": 0.0},
                        },
                    ],
                    type=dtype,
                ),
            }
        ),
        "sample_id",
    )
    values = {r["sample_id"]: r[name] for r in ds.to_table(columns=["sample_id", name]).to_pylist()}
    assert values[rows[1]["sample_id"]] is None
    assert values[rows[3]["sample_id"]]["status"] == "failed"
    assert values[rows[3]["sample_id"]]["result"] is None
    assert values[rows[2]["sample_id"]]["result"]["score"] == 0.0
    assert ds.to_table(columns=["sample_id"], filter=f"{name}.result.score > 1").num_rows == 1
    assert name not in lance.dataset(path, version=base_version).schema.names
    for path, (mtime, checksum) in original_files.items():
        assert path.stat().st_mtime_ns == mtime
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    target = rows[2]["sample_id"]
    scanner = ds.scanner(columns=["sample_id", "audio"], filter=f"sample_id = '{target}'")
    assert "ScalarIndex" in scanner.explain_plan()
    assert scanner.to_table().to_pylist()[0]["audio"]["bytes"] == rows[2]["audio"]["bytes"]
