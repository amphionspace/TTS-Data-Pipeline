"""Lance 12 same-dataset branch safety, on disposable local tables only."""

from datetime import timedelta

import lance
import pyarrow as pa
import pytest


def base(tmp_path):
    path = tmp_path / "samples.lance"
    table = pa.table({"sample_id": ["a", "b", "c"], "audio": [b"first", b"second", b"third"]})
    ds = lance.write_dataset(table, path, data_storage_version="2.2")
    ds.create_scalar_index("sample_id", "BTREE")
    return path, ds, table


def test_branch_only_reference_survives_main_cleanup(tmp_path):
    path, ds, original = base(tmp_path)
    branch = ds.create_branch("selection", ds.version)
    branch.add_columns({"selection_reason": "cast(0 as smallint unsigned)"})
    selected_version = branch.version
    # No tag on base or branch: only branch ancestry protects original audio.
    replacement = pa.table({"sample_id": ["new"], "audio": [b"new"]})
    main = lance.write_dataset(replacement, path, mode="overwrite", data_storage_version="2.2")
    main.cleanup_old_versions(older_than=timedelta(0))
    pinned = main.checkout_version(("selection", selected_version))
    assert pinned.to_table(columns=original.column_names) == original
    assert pinned.count_rows(filter="sample_id = 'b'") == 1
    assert main.to_table() == replacement


def test_tagged_branch_snapshot_survives_branch_cleanup(tmp_path):
    path, ds, original = base(tmp_path)
    branch = ds.create_branch("selection", ds.version)
    branch.add_columns({"selection_reason": "cast(0 as smallint unsigned)"})
    version = branch.version
    ds.tags.create("keep-selection", ("selection", version))
    branch.update({"selection_reason": "cast(1001 as smallint unsigned)"}, where="sample_id = 'b'")
    branch.cleanup_old_versions(older_than=timedelta(0), error_if_tagged_old_versions=False)
    pinned = lance.dataset(path).checkout_version(("selection", version))
    assert pinned.to_table(columns=original.column_names) == original
    assert pinned.count_rows(filter="selection_reason = 0") == 3
    assert branch.count_rows(filter="selection_reason = 0") == 2


def test_interrupted_add_columns_does_not_publish_partial_schema(tmp_path):
    _, ds, original = base(tmp_path)
    branch = ds.create_branch("selection", ds.version)
    version = branch.version
    schema = pa.schema([("selection_reason", pa.uint16())])
    calls = 0

    @lance.batch_udf(output_schema=schema)
    def fail(batch):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected interruption")
        return pa.record_batch([pa.array([0] * batch.num_rows, type=pa.uint16())], schema=schema)

    with pytest.raises(Exception, match="injected interruption"):
        branch.add_columns(fail, read_columns=["sample_id"], batch_size=1)
    head = ds.checkout_version(("selection", None))
    assert head.version == version
    assert head.to_table() == original
    head.add_columns({"selection_reason": "cast(0 as smallint unsigned)"})
    assert head.count_rows(filter="selection_reason = 0") == 3


def test_tag_prevents_branch_deletion(tmp_path):
    _, ds, _ = base(tmp_path)
    branch = ds.create_branch("selection", ds.version)
    ds.tags.create("published-selection", ("selection", branch.version))
    with pytest.raises(OSError, match="referenced by tags"):
        ds.branches.delete("selection")
    assert ds.checkout_version(("selection", branch.version)).count_rows() == 3


def test_feature_locator_requires_join_and_pinned_snapshot(tmp_path):
    _, ds, _ = base(tmp_path)
    selected = ds.create_branch("selection", ds.version)
    selected.add_columns({"selection_reason": "cast(0 as smallint unsigned)"})
    features_path = tmp_path / "features.lance"
    features = lance.write_dataset(
        pa.table({"target_id": ["c", "a"], "codes": [[3, 4], [1, 2]]}), features_path
    )
    version = features.version
    # The deliberately reordered output is matched once, not presumed row-aligned.
    locations = {v: i for i, v in enumerate(features.to_table()["target_id"].to_pylist())}
    binding = selected.create_branch("build", selected.version)
    binding.merge(
        pa.table(
            {
                "sample_id": ["b", "c", "a"],
                "codec_row": pa.array([locations.get(k) for k in ["b", "c", "a"]], type=pa.int64()),
            }
        ),
        left_on="sample_id",
    )
    binding.add_columns({"build_ready": "codec_row is not null"})
    ready = binding.to_table(columns=["sample_id", "codec_row"], filter="build_ready")
    lance.write_dataset(
        pa.table({"target_id": ["new"], "codes": [[9, 9]]}), features_path, mode="overwrite"
    )
    pinned = lance.dataset(features_path, version=version)
    actual = pinned.take(ready["codec_row"].to_pylist())
    assert actual["target_id"].to_pylist() == ready["sample_id"].to_pylist()
    assert binding.count_rows(filter="build_ready") == 2
    assert selected.count_rows(filter="selection_reason = 0") == 3
