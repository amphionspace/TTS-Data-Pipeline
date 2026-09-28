import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tts_data_pipeline.inventory import audit


def test_missing_shards_and_orphan_sidecars(tmp_path):
    root = tmp_path / "raw"
    corpus = root / "tiny"
    corpus.mkdir(parents=True)
    pq.write_table(pa.table({"text": ["你好", None]}), corpus / "train-00000-of-00002.parquet")
    (corpus / "train-00001-of-00002.parquet.sha256").write_text("unknown")
    result = audit(root, tmp_path / "report")["datasets"]["tiny"]
    assert result["counts"]["parquet_rows"] == 2
    kinds = {issue["kind"] for issue in result["issues"]}
    assert kinds == {"orphan_sha256_sidecar", "parquet_shard_sequence"}
    with pytest.raises(ValueError, match="outside"):
        audit(root, root / "report")
