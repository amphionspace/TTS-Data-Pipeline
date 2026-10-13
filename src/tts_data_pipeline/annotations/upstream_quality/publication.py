"""Publish one upstream-quality row per base sample of a dataset (sample_table)."""

import hashlib
import json
import shutil
from pathlib import Path

import lance
import pyarrow as pa

from ...contract import schema_description
from ...feature_runtime import write_json
from ...schema import digest
from .schema import annotation_schema

TASK = "upstream_quality"


def equal_batches(left, right):
    """Compare every logical value while allowing different physical batch boundaries."""
    left, right = iter(left), iter(right)
    a, b = next(left, None), next(right, None)
    while a is not None and b is not None:
        n = min(len(a), len(b))
        if not a.slice(0, n).equals(b.slice(0, n)):
            return False
        a = a.slice(n) if len(a) > n else next(left, None)
        b = b.slice(n) if len(b) > n else next(right, None)
    return a is None and b is None


def publish_output(root, source, run_id, work, batches, counts, stats):
    """Stage, write and fully read back one dataset's table, then expose a pinned snapshot."""
    root, work = Path(root), Path(work)
    name = source["dataset_id"]
    final = root / "datasets" / name / source["release_id"] / "annotations" / TASK / run_id
    stage = final.with_name(final.name + ".incomplete")
    receipt = work / (name + ".publication.json")
    if sum(counts.values()) != source["rows"]:
        raise ValueError("Publication coverage mismatch")
    if receipt.exists():
        output = json.loads(receipt.read_text())
        if not final.exists():
            stage.rename(final)
    else:
        if final.exists():
            raise FileExistsError("Unrecognized existing annotation output")
        if stage.exists():
            # An earlier attempt crashed before its receipt: this run-owned stage was never
            # published, so start clean rather than stacking versions onto stale files.
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        schema = annotation_schema()

        def write_batches():
            # Lance 12's struct encoder mishandles sliced nullable-struct bitmaps.
            for batch in batches():
                yield batch.take(pa.array(range(len(batch)), type=pa.int64()))

        ds = lance.write_dataset(
            pa.RecordBatchReader.from_batches(schema, write_batches()),
            stage / "results.lance",
            mode="overwrite",
            data_storage_version="2.2",
            max_rows_per_file=1 << 20,
        )
        if ds.count_rows() != source["rows"]:
            raise ValueError("Published row count mismatch")
        if not equal_batches(batches(), ds.scanner(scan_in_order=True).to_batches()):
            raise ValueError("Published content readback mismatch")
        checksum, identity_rows = hashlib.sha256(), 0
        for batch in ds.scanner(
            columns=["sample_id"], batch_size=65536, scan_in_order=True
        ).to_batches():
            ids = batch.column(0)
            if ids.null_count:
                raise ValueError("Published null sample identity")
            checksum.update(b"".join(s.encode("ascii") for s in ids.to_pylist()))
            identity_rows += len(ids)
        if identity_rows != source["rows"] or checksum.hexdigest() != source["ordered_ids_sha256"]:
            raise ValueError("Published identities differ from pinned base")
        if identity_rows:
            ds.create_scalar_index("sample_id", "BTREE")
        tag = "annotation-" + run_id
        if tag in ds.tags.list():
            ds.tags.update(tag, ds.version)
        else:
            ds.tags.create(tag, ds.version)
        output = dict(
            base_input_alias=source["alias"],
            dataset_id=name,
            release_id=source["release_id"],
            storage_kind="sample_table",
            layout_reason="one_row_per_target_including_failures",
            table=dict(
                table_path=str((final / "results.lance").relative_to(root)),
                branch=None,
                lance_version=ds.version,
                tag=tag,
                rows=identity_rows,
                schema=schema_description(schema),
                schema_sha256=digest(schema_description(schema)),
            ),
            coverage=dict(
                total_targets=source["rows"],
                missing=0,
                **{s: counts.get(s, 0) for s in ("ok", "failed", "unsupported", "skipped")},
            ),
            statistics=stats,
            validation=dict(
                readback_rows=identity_rows,
                unique_targets=identity_rows,
                identity_basis="ordered_ids_equal_pinned_base_validated_unique_at_publication",
                ordered_ids_sha256=source["ordered_ids_sha256"],
                all_fields_equal_source=True,
            ),
        )
        write_json(receipt, output)
        stage.rename(final)
    table = output["table"]
    ds = lance.dataset(root / table["table_path"], version=table["lance_version"])
    if (
        ds.count_rows() != source["rows"]
        or digest(schema_description(ds.schema)) != table["schema_sha256"]
    ):
        raise ValueError("Output snapshot changed")
    return output
