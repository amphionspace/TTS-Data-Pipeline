"""Verified private checkpoints and atomic, immutable sample-table publication."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.parquet as pq

from ...contract import schema_description
from ...feature_runtime import file_hash, write_json
from ...schema import digest
from ...timestamps import BEIJING
from .schema import annotation_schema


def equal_batches(left, right):
    """Compare every Arrow value, independent of scanner batch boundaries."""
    left, right = iter(left), iter(right)
    a, b = next(left, None), next(right, None)
    while a is not None and b is not None:
        n = min(len(a), len(b))
        if not a.slice(0, n).equals(b.slice(0, n)):
            return False
        a = a.slice(n) if len(a) > n else next(left, None)
        b = b.slice(n) if len(b) > n else next(right, None)
    return a is None and b is None


def load_checkpoint(work, task, plan_hash):
    folder = Path(work) / "checkpoints" / task["id"]
    path = folder / "checkpoint.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value["task"] != task or value["plan_sha256"] != plan_hash:
        raise ValueError("Checkpoint identity changed")
    if file_hash(folder / "results.parquet") != value["sha256"]:
        raise ValueError("Checkpoint content changed")
    return value


def batches(work, tasks):
    for task in tasks:
        path = Path(work) / "checkpoints" / task["id"] / "results.parquet"
        yield from pq.ParquetFile(path).iter_batches(batch_size=4096)


def publish(plan, work, checkpoints):
    root = Path(plan["output_root"])
    outputs = []
    schema = annotation_schema()
    description = schema_description(schema)
    for index, source in enumerate(plan["inputs"]):
        tasks = [t for t in plan["tasks"] if t["source"] == index]
        counts, errors = Counter(), Counter()
        for task in tasks:
            counts.update(checkpoints[task["id"]]["counts"])
            errors.update(checkpoints[task["id"]]["errors"])
        if sum(counts.values()) != source["target_rows"]:
            raise ValueError("Incomplete source coverage")
        final = (
            root
            / "datasets"
            / source["dataset_id"]
            / source["release_id"]
            / "annotations/spoken_language"
            / plan["run_id"]
        )
        stage = final.with_name(final.name + ".incomplete")
        receipt = Path(work) / "publications" / (source["dataset_id"] + ".json")
        if receipt.exists():
            output = json.loads(receipt.read_text())
            if not final.exists():
                stage.rename(final)
        else:
            if final.exists():
                raise FileExistsError("Unrecognized existing language annotation")
            stage.mkdir(parents=True, exist_ok=True)
            # Materialize nullable structs: Lance 12 cannot encode sliced validity buffers.
            reader = pa.RecordBatchReader.from_batches(
                schema,
                (b.take(pa.array(range(len(b)), type=pa.int64())) for b in batches(work, tasks)),
            )
            ds = lance.write_dataset(
                reader,
                stage / "results.lance",
                mode="overwrite",
                data_storage_version="2.2",
                max_rows_per_file=262144,
            )
            if ds.count_rows() != source["target_rows"] or not equal_batches(
                batches(work, tasks), ds.scanner(scan_in_order=True).to_batches()
            ):
                raise ValueError("Language annotation full-value readback failed")
            ds.create_scalar_index("sample_id", "BTREE")
            tag = "annotation-" + plan["run_id"]
            ds.tags.create(tag, ds.version)
            output = dict(
                base_input_alias=source["alias"],
                dataset_id=source["dataset_id"],
                release_id=source["release_id"],
                storage_kind="sample_table",
                layout_reason="one_row_per_sample_including_failures",
                table=dict(
                    table_path=str((final / "results.lance").relative_to(root)),
                    branch=None,
                    lance_version=ds.version,
                    tag=tag,
                    rows=source["target_rows"],
                    schema=description,
                    schema_sha256=digest(description),
                ),
                coverage=dict(
                    total_targets=source["target_rows"],
                    missing=0,
                    **{k: counts[k] for k in ("ok", "failed", "unsupported", "skipped")},
                ),
                error_counts=dict(errors),
                validation=dict(
                    readback_rows=source["target_rows"],
                    identity="checkpoint_ids_equal_pinned_unique_base_ranges",
                ),
            )
            write_json(receipt, output)
            stage.rename(final)
        table = output["table"]
        ds = lance.dataset(root / table["table_path"], version=table["lance_version"])
        if (
            ds.count_rows() != table["rows"]
            or digest(schema_description(ds.schema)) != table["schema_sha256"]
        ):
            raise ValueError("Published snapshot changed")
        outputs.append(output)
    manifest = dict(
        artifact_kind="annotation",
        contract_version="v0.1",
        schema_version="firered-lid-v1",
        status="complete",
        task="spoken_language",
        target_kind="sample",
        run_id=plan["run_id"],
        profile=plan["profile"],
        profile_id=plan["profile_id"],
        scope=plan["scope"],
        inputs=plan["inputs"],
        outputs=outputs,
        finished_at=datetime.now(BEIJING).isoformat(),
        execution=json.loads((Path(work) / "execution.json").read_text()),
    )
    if plan["root"] != plan["output_root"]:
        manifest["experimental_input_root"] = plan["root"]
    path = root / "annotations/spoken_language" / plan["run_id"] / "manifest.json"
    if path.exists():
        raise FileExistsError("Published annotation is immutable")
    write_json(path, manifest)
    return path
