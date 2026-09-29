"""Verify the proposed feature payloads survive real Lance writes and indexed reads."""

import json
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.contract import codec_schema, speaker_embedding_schema


@pytest.mark.parametrize("kind", ["codec", "speaker"])
def test_feature_arrays_and_failed_nulls_roundtrip_with_snapshot_index(tmp_path, kind):
    examples = Path(__file__).resolve().parents[1] / "docs/data-contract/examples"
    source = json.loads((examples / f"{kind}-row.example.json").read_text())
    schema = codec_schema(2) if kind == "codec" else speaker_embedding_schema(3)
    row = {k: source[k] for k in schema.names}
    row.update(target_id="target_ok", feature_key="content_ok")
    failed = dict(
        row,
        target_id="target_failed",
        feature_key="content_failed",
        status="failed",
        error_code="encode_failed",
    )
    payload, checksum = (
        ("codes", "codes_sha256") if kind == "codec" else ("embedding", "embedding_sha256")
    )
    failed[payload] = failed[checksum] = None
    if kind == "codec":
        failed["num_codec_frames"] = None
    path = tmp_path / "features.lance"
    table = pa.Table.from_pylist([row, failed], schema=schema)
    ds = lance.write_dataset(table, path, data_storage_version="2.2")
    ds.create_scalar_index("target_id", "BTREE")
    ds.create_scalar_index("feature_key", "BTREE")
    version = ds.version
    pinned = lance.dataset(path, version=version)
    actual = pinned.to_table(filter="target_id = 'target_ok'").to_pylist()
    assert actual == [row]
    assert pinned.to_table(filter="target_id = 'target_failed'").to_pylist() == [failed]
    extra = dict(row, target_id="another", feature_key="another_content")
    lance.write_dataset(pa.Table.from_pylist([extra], schema=schema), path, mode="append")
    assert lance.dataset(path).count_rows() == 3
    assert lance.dataset(path, version=version).count_rows() == 2
