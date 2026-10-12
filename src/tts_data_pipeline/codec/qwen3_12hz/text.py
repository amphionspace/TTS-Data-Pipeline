"""Add pinned selection text to codec tables without rewriting audio codes."""

import hashlib
import json
import multiprocessing
import os
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lance
import pyarrow as pa

from ...contract import codec_text_schema
from ...text.metadata import SOURCE_COLUMNS, annotation_revisions, selected_metadata

# Rows per worker unit; the audit chain is fed in task order, so workers only shape bytes.
CHUNK_ROWS = 65536
_CODEC = "lz4"
_WORKER = {}
_DATASETS = {}


def _row_line(target_id, metadata):
    """One canonical audit line; the ordered chain hashes these bytes in row order."""
    return (
        json.dumps(
            [target_id, metadata],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def _row_hash(checksum, target_id, metadata):
    checksum.update(_row_line(target_id, metadata))


def default_workers():
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    return max(1, min(32, cpus))


def _task_metadata(task, selected, text_sources, revisions):
    """Verify one task's selected rows and return their metadata in task order."""
    ids = [r["sample_id"] for r in selected]
    if (
        len(ids) != task["rows"]
        or hashlib.sha256("".join(ids).encode()).hexdigest() != task["ordered_ids_sha256"]
    ):
        raise ValueError("Selected metadata target set changed")
    annotated = [r["sample_id"] for r in selected if r["selected_text_source"]]
    raw = {}
    if annotated:
        if revisions is None:
            raise ValueError("Annotation text requires its pinned raw revision")
        code, raw = revisions(annotated)
        if any(r["selected_text_source"] != code for r in selected if r["sample_id"] in raw):
            raise ValueError("Selected text source differs from the pinned annotation")
    return [selected_metadata(row, text_sources, raw.get(row["sample_id"])) for row in selected]


def _records(table):
    """Row dicts equal to table.to_pylist(), converted column-wise (several times faster)."""
    names = table.column_names
    columns = [table.column(name).to_pylist() for name in names]
    return [dict(zip(names, values)) for values in zip(*columns)]


def _pack(lines):
    payload = b"".join(lines)
    return pa.compress(payload, codec=_CODEC, asbytes=True), len(payload)


def _unpack(packed):
    compressed, size = packed
    return pa.decompress(compressed, size, codec=_CODEC, asbytes=True)


def _locator(dataset):
    """(uri, branch, version) reopening exactly this pinned snapshot in another process.

    Lance pickles a dataset by manifest alone, which drops the branch directory its data
    files live in, so the pinned version is located by name on main or on a branch instead.
    """
    manifest = dataset._ds.serialized_manifest()
    for branch in [None, *lance.dataset(dataset.uri).branches.list()]:
        try:
            candidate = _open((dataset.uri, branch, dataset.version), cache=False)
        except Exception:
            continue
        if candidate._ds.serialized_manifest() == manifest:
            return (dataset.uri, branch, dataset.version)
    raise ValueError("Pinned dataset version is not reachable by name for worker processes")


def _open(locator, cache=True):
    if not cache or locator not in _DATASETS:
        uri, branch, version = locator
        dataset = lance.dataset(uri)
        if branch is None:
            dataset = dataset.checkout_version(version)
        else:
            dataset = dataset.checkout_version((branch, version))
        if not cache:
            return dataset
        _DATASETS[locator] = dataset
    return _DATASETS[locator]


def _worker_init(state):
    _WORKER.clear()
    _DATASETS.clear()
    _WORKER.update(state)


def _worker_revisions():
    """Rebuild the annotation lookup lazily so a bad selection surfaces as a task error."""
    if "revisions" not in _WORKER:
        revisions = None
        if _WORKER["selection_path"] is not None:
            selection = json.loads(Path(_WORKER["selection_path"]).read_text())
            revisions = annotation_revisions(_WORKER["root"], selection, _WORKER["dataset_id"])
        _WORKER["revisions"] = revisions
    return _WORKER["revisions"]


def _materialize_chunk(chunk):
    """Metadata table, audit lines and ID-order check for contiguous tasks of one fragment."""
    source, pinned = _open(_WORKER["source"]), _open(_WORKER["pinned"])
    tasks = chunk["tasks"]
    span = sum(t["rows"] for t in tasks)
    rows = source.scanner(
        fragments=[source.get_fragment(chunk["fragment"])],
        columns=SOURCE_COLUMNS,
        filter="selection_reason = 0",
        offset=tasks[0]["offset"],
        limit=span,
        scan_in_order=True,
    ).to_table()
    if rows.num_rows != span:
        raise ValueError("Selected metadata target set changed")
    revisions = _worker_revisions() if _WORKER["annotations"] else None
    records = _records(rows)
    metadata, position = [], 0
    for task in tasks:
        selected = records[position : position + task["rows"]]
        position += task["rows"]
        metadata.extend(_task_metadata(task, selected, _WORKER["text_sources"], revisions))
    ids = [r["sample_id"] for r in records]
    expected = pinned.scanner(
        columns=["target_id"], offset=chunk["start"], limit=span, scan_in_order=True
    ).to_table()
    if expected.column("target_id").to_pylist() != ids:
        raise ValueError("Codec/text target order differs")
    lines = [_row_line(i, m) for i, m in zip(ids, metadata)]
    table = pa.Table.from_pylist(metadata, schema=codec_text_schema())
    return table, _pack(lines), span


def _verify_chunk(unit):
    """Audit lines for stored rows, read as (fragment, offset, limit) ranges in table order."""
    locator, ranges = unit
    features = _open(locator)
    names = ["target_id", *codec_text_schema().names]
    lines, count = [], 0
    for fragment_id, offset, limit in ranges:
        table = features.scanner(
            fragments=[features.get_fragment(fragment_id)],
            columns=names,
            offset=offset,
            limit=limit,
            scan_in_order=True,
        ).to_table()
        for row in _records(table):
            lines.append(_row_line(row.pop("target_id"), row))
            count += 1
    return _pack(lines), count


def _chunks(tasks, chunk_rows):
    """Group tasks in order: same fragment, contiguous offsets, about chunk_rows rows."""
    chunks, start = [], 0
    for task in tasks:
        current = chunks[-1] if chunks else None
        if (
            current is None
            or current["fragment"] != task["fragment"]
            or current["rows"] >= chunk_rows
            or current["tasks"][-1]["offset"] + current["tasks"][-1]["rows"] != task["offset"]
        ):
            current = {"fragment": task["fragment"], "start": start, "rows": 0, "tasks": []}
            chunks.append(current)
        current["tasks"].append(
            {k: task[k] for k in ("fragment", "offset", "rows", "ordered_ids_sha256")}
        )
        current["rows"] += task["rows"]
        start += task["rows"]
    return chunks


def _verify_ranges(features, chunk_rows):
    """Row ranges over the stored fragments, in table order, about chunk_rows per unit."""
    groups, current, size = [], [], 0
    for fragment in features.get_fragments():
        offset, remaining = 0, fragment.count_rows()
        while remaining:
            take = min(remaining, chunk_rows - size)
            current.append((fragment.fragment_id, offset, take))
            offset, remaining, size = offset + take, remaining - take, size + take
            if size >= chunk_rows:
                groups.append(current)
                current, size = [], 0
    if current:
        groups.append(current)
    return groups


def _ordered(pool, function, items, lookahead):
    """Results in submission order with at most `lookahead` units in flight."""
    pending, items = deque(), iter(items)

    def fill():
        while len(pending) < lookahead:
            item = next(items, None)
            if item is None:
                return
            pending.append(pool.submit(function, item))

    fill()
    while pending:
        result = pending.popleft().result()
        fill()
        yield result


def _pool(workers, state):
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_worker_init,
        initargs=(state,),
    )


def _sequential(features, source, tasks, text_sources, revisions, schema, expected_hash):
    """Original single-process materialization; returns the row count it wrote."""
    total = 0
    pinned = lance.dataset(features.uri, version=features.version)

    def targets():
        for batch in pinned.scanner(columns=["target_id"], scan_in_order=True).to_batches():
            yield from batch.column("target_id").to_pylist()

    actual_ids = iter(targets())

    def batches():
        nonlocal total
        current_fragment, rows = None, None
        for task in tasks:
            if current_fragment != task["fragment"]:
                current_fragment = task["fragment"]
                rows = source.scanner(
                    fragments=[source.get_fragment(current_fragment)],
                    columns=SOURCE_COLUMNS,
                    filter="selection_reason = 0",
                    scan_in_order=True,
                ).to_table()
            selected = rows.slice(task["offset"], task["rows"]).to_pylist()
            result = _task_metadata(task, selected, text_sources, revisions)
            for row, metadata in zip(selected, result):
                if next(actual_ids, None) != row["sample_id"]:
                    raise ValueError("Codec/text target order differs")
                _row_hash(expected_hash, row["sample_id"], metadata)
                total += 1
            yield from pa.Table.from_pylist(result, schema=schema).to_batches(max_chunksize=4096)
        if next(actual_ids, None) is not None:
            raise ValueError("Codec table has extra targets")

    features.add_columns(pa.RecordBatchReader.from_batches(schema, batches()), batch_size=4096)
    actual_hash = hashlib.sha256()
    count = 0
    for batch in features.scanner(
        columns=["target_id", *schema.names], scan_in_order=True, batch_size=8192
    ).to_batches():
        for row in batch.to_pylist():
            target = row.pop("target_id")
            _row_hash(actual_hash, target, row)
            count += 1
    return total, count, actual_hash


def _parallel(
    features,
    source,
    tasks,
    text_sources,
    revisions,
    schema,
    expected_hash,
    workers,
    selection_path,
    root,
    dataset_id,
    chunk_rows,
    lookahead,
):
    """Spawned workers shape per-task metadata and audit bytes; this process keeps the order."""
    if revisions is not None and selection_path is None:
        raise ValueError("Parallel text materialization needs selection_path to rebuild revisions")
    chunks = _chunks(tasks, chunk_rows)
    planned = sum(c["rows"] for c in chunks)
    stored = features.count_rows()
    if stored > planned:
        raise ValueError("Codec table has extra targets")
    if stored < planned:
        raise ValueError("Codec/text target order differs")
    state = dict(
        source=_locator(source),
        pinned=_locator(features),
        text_sources=text_sources,
        annotations=revisions is not None,
        selection_path=None if selection_path is None else str(selection_path),
        root=None if root is None else str(root),
        dataset_id=dataset_id,
    )
    total = 0

    def batches(results):
        nonlocal total
        for table, packed, rows in results:
            expected_hash.update(_unpack(packed))
            total += rows
            yield from table.to_batches(max_chunksize=4096)

    actual_hash = hashlib.sha256()
    count = 0
    pool = _pool(workers, state)
    try:
        results = _ordered(pool, _materialize_chunk, chunks, lookahead)
        features.add_columns(
            pa.RecordBatchReader.from_batches(schema, batches(results)), batch_size=4096
        )
        stored = _locator(features)
        units = [(stored, ranges) for ranges in _verify_ranges(features, chunk_rows)]
        for packed, rows in _ordered(pool, _verify_chunk, units, lookahead):
            actual_hash.update(_unpack(packed))
            count += rows
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return total, count, actual_hash


def materialize_text(
    features,
    source,
    tasks,
    text_sources,
    revisions=None,
    *,
    workers=None,
    selection_path=None,
    root=None,
    dataset_id=None,
    chunk_rows=CHUNK_ROWS,
    lookahead=None,
):
    """Verify each destination ID before adding metadata, then audit stored columns.

    `revisions(ids)` returns (text_source, {sample_id: raw text}) for annotation-sourced rows.
    With `workers` (a count, or "auto" for min(32, cpus)) spawned processes compute each
    task's metadata and audit bytes while this process feeds them to the table and the
    ordered hash in task order. Workers rebuild `revisions` from `selection_path`, `root`
    and `dataset_id`, so those are required whenever `revisions` is given. Without
    `workers` the original single-process path runs; both yield identical columns and audit.
    """
    schema = codec_text_schema()
    if set(schema.names) & set(features.schema.names):
        raise ValueError("Text materialization requires a fresh unpublished codec snapshot")
    original_files = {
        f.path for fragment in features.get_fragments() for f in fragment.metadata.files
    }
    expected_hash = hashlib.sha256()
    if workers is None:
        total, count, actual_hash = _sequential(
            features, source, tasks, text_sources, revisions, schema, expected_hash
        )
    else:
        workers = default_workers() if workers == "auto" else int(workers)
        if workers < 1:
            raise ValueError("Text materialization needs at least one worker")
        total, count, actual_hash = _parallel(
            features,
            source,
            tasks,
            text_sources,
            revisions,
            schema,
            expected_hash,
            workers,
            selection_path,
            root,
            dataset_id,
            max(1, int(chunk_rows)),
            2 * workers if lookahead is None else max(1, int(lookahead)),
        )
    files = {f.path for fragment in features.get_fragments() for f in fragment.metadata.files}
    if (
        count != total
        or actual_hash.digest() != expected_hash.digest()
        or not original_files <= files
    ):
        raise ValueError("Materialized text roundtrip or original codec files changed")
    return {
        "mode": "selection_text_columns_v1",
        "columns": schema.names,
        "rows": count,
        "ordered_text_sha256": actual_hash.hexdigest(),
        "text_sources": text_sources,
        "original_codec_files_preserved": True,
    }
