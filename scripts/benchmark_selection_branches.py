"""Read-only source projection and branch benchmarks, strictly under /tmp.

Synthetic decisions exercise storage, not a production cleaning policy. The large
case projects metadata only; global duplicate discovery is not benchmarked here.
"""

import argparse
import hashlib
import json
import resource
import time
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc


def checksums(path):
    result = {}
    for file in (path / "data").glob("*.lance"):
        with file.open("rb") as stream:
            result[file.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def decisions(batch):
    # Intentionally synthetic: verify a nonconstant result without changing text/audio.
    duration = batch.column("duration_seconds")
    short = pc.less(duration, 3.0)
    reason = pc.cast(pc.if_else(short, 3001, 0), pa.uint16())
    flags = pc.cast(pc.if_else(short, 1, 0), pa.uint32())
    return pa.record_batch([reason, flags], names=["selection_reason", "selection_flags"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "merge", "add_columns"])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--with-audio", action="store_true")
    args = parser.parse_args()
    work = args.work.resolve()
    if not work.is_relative_to(Path("/tmp")):
        raise ValueError("Benchmark output must be under /tmp")
    work.mkdir(parents=True, exist_ok=True)
    path = work / "samples.lance"
    started = time.monotonic()
    if args.mode == "prepare":
        manifest_bytes = (args.source / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        ds = lance.dataset(args.source / manifest["table_path"], version=manifest["lance_version"])
        columns = ["sample_id", "duration_seconds"]
        if args.with_audio:
            columns += ["audio", "text", "audio_sha256"]
        scanner = ds.scanner(columns=columns, batch_size=128 if args.with_audio else 65536)
        local = lance.write_dataset(
            scanner.to_reader(),
            path,
            data_storage_version="2.2",
            max_rows_per_file=1_000_000,
            max_bytes_per_file=1024**3,
        )
        local.create_scalar_index("sample_id", "BTREE")
        info = {
            "source": str(args.source),
            "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "source_version": manifest["lance_version"],
            "columns": columns,
            "rows": local.count_rows(),
            "base_version": local.version,
            "base_file_hashes": checksums(path),
        }
        assert info["rows"] == manifest["rows"]
        (work / "input.json").write_text(json.dumps(info, indent=2))
        result = info | {"mode": "prepare"}
    else:
        info = json.loads((work / "input.json").read_text())
        ds = lance.dataset(path, version=info["base_version"])
        branch = ds.create_branch(args.mode, info["base_version"])
        schema = pa.schema([("selection_reason", pa.uint16()), ("selection_flags", pa.uint32())])
        operation_started = time.monotonic()
        if args.mode == "add_columns":
            fn = lance.batch_udf(output_schema=schema)(decisions)
            branch.add_columns(fn, read_columns=["duration_seconds"], batch_size=65536)
        else:

            def batches():
                for batch in ds.to_batches(
                    columns=["sample_id", "duration_seconds"], batch_size=65536
                ):
                    extra = decisions(batch)
                    joined = pa.record_batch(
                        [batch.column("sample_id"), *extra.columns],
                        names=["sample_id", *schema.names],
                    )
                    # Reverse each batch: row order must not be assumed by merge.
                    yield joined.take(pa.array(np.arange(batch.num_rows - 1, -1, -1)))

            right_schema = pa.schema([("sample_id", pa.string()), *schema])
            branch.merge(
                pa.RecordBatchReader.from_batches(right_schema, batches()), left_on="sample_id"
            )
        operation_seconds = time.monotonic() - operation_started
        rows = selected = 0
        for batch in branch.to_batches(
            columns=["duration_seconds", *schema.names], batch_size=65536
        ):
            expected = decisions(batch)
            for name in schema.names:
                assert batch.column(name).equals(expected.column(name)), name
            rows += batch.num_rows
            selected += pc.sum(
                pc.cast(pc.equal(batch.column("selection_reason"), 0), pa.int64())
            ).as_py()
        assert rows == info["rows"]
        assert branch.count_rows(filter="selection_reason = 0") == selected
        assert lance.dataset(path).version == info["base_version"]
        assert checksums(path) == info["base_file_hashes"]
        base_names = set(info["base_file_hashes"])
        inherited = {
            f.path
            for frag in branch.get_fragments()
            for f in frag.data_files()
            if f.path in base_names
        }
        assert inherited == base_names
        assert not any(p.name in base_names for p in Path(branch.uri).rglob("*.lance"))
        result = {
            "mode": args.mode,
            "rows": rows,
            "selected": selected,
            "branch_version": branch.version,
            "operation_seconds": operation_seconds,
            "branch_bytes": sum(
                p.stat().st_size for p in Path(branch.uri).rglob("*") if p.is_file()
            ),
            "base_files_unchanged": True,
            "full_column_validation": True,
        }
    result.update(
        elapsed_seconds=time.monotonic() - started,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        pylance=lance.__version__,
    )
    (work / f"{args.mode}.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "base_file_hashes"}), flush=True)


if __name__ == "__main__":
    main()
