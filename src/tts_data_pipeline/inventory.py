"""Read-only filesystem and Parquet footer audit; never decode all audio."""

import gzip
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

SHARD = re.compile(r"^(.*)-(\d+)-of-(\d+)\.parquet$")


def audit(root: Path, output: Path) -> dict:
    root = root.resolve(strict=True)
    output = output.resolve()
    if output == root or root in output.parents:
        raise ValueError("Audit output must be outside the raw data root")
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "scope": "All regular file stats and Parquet footers; no full hashes or audio decoding",
        "datasets": {},
    }
    with gzip.open(output / "files.jsonl.gz", "wt", encoding="utf-8") as inventory:
        for dataset in sorted(root.iterdir()):
            if not dataset.is_dir() or dataset.is_symlink():
                continue
            counts, extensions, groups, schemas = Counter(), Counter(), {}, {}
            issues = []
            for directory, dirs, files in os.walk(dataset, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not Path(directory, d).is_symlink())
                for name in sorted(files):
                    path = Path(directory, name)
                    rel = str(path.relative_to(root))
                    if path.is_symlink():
                        issues.append({"path": rel, "kind": "symlink_skipped"})
                        continue
                    try:
                        stat = path.stat()
                        row = {"path": rel, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                        counts["files"] += 1
                        counts["bytes"] += stat.st_size
                        extensions[path.suffix or "<none>"] += 1
                        if stat.st_size == 0:
                            issues.append({"path": rel, "kind": "empty_file"})
                        if name.endswith((".part", ".partial", ".tmp")):
                            issues.append({"path": rel, "kind": "partial_file"})
                        if name.endswith(".sha256"):
                            counts["sha256_sidecars"] += 1
                            if not path.with_suffix("").is_file():
                                issues.append({"path": rel, "kind": "orphan_sha256_sidecar"})
                        if path.suffix == ".parquet":
                            pf = pq.ParquetFile(path)
                            schema = str(pf.schema_arrow.remove_metadata())
                            fingerprint = hashlib.sha256(schema.encode()).hexdigest()
                            schemas.setdefault(fingerprint, {"schema": schema, "files": 0})
                            schemas[fingerprint]["files"] += 1
                            row.update(rows=pf.metadata.num_rows, schema=fingerprint)
                            counts["parquet_files"] += 1
                            counts["parquet_rows"] += pf.metadata.num_rows
                            counts["parquet_bytes"] += stat.st_size
                            match = SHARD.match(name)
                            if match:
                                stem, idx, total = match.groups()
                                key = (str(path.parent.relative_to(root)), stem, int(total))
                                groups.setdefault(key, set()).add(int(idx))
                        inventory.write(json.dumps(row, ensure_ascii=False) + "\n")
                    except Exception as exc:
                        issues.append({"path": rel, "kind": "read_error", "error": str(exc)})
            for (directory, stem, total), indices in sorted(groups.items()):
                missing = sorted(set(range(total)) - indices)
                extra = sorted(indices - set(range(total)))
                if missing or extra:
                    issues.append(
                        {
                            "path": directory,
                            "kind": "parquet_shard_sequence",
                            "stem": stem,
                            "expected": total,
                            "missing": missing,
                            "out_of_range": extra,
                        }
                    )
            summary["datasets"][dataset.name] = {
                "counts": dict(counts),
                "extensions": dict(sorted(extensions.items())),
                "schemas": schemas,
                "issues": issues,
            }
            print(
                f"{dataset.name}: {counts['files']} files; "
                f"{counts['parquet_rows']} Parquet rows; {len(issues)} issues",
                flush=True,
            )
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def scalar_audit(root: Path, output: Path) -> dict:
    """Scan selected text columns only; missing means null or whitespace-only."""
    import pyarrow as pa
    import pyarrow.compute as pc

    selected = {
        "text",
        "transcription",
        "text_original",
        "text_normalized",
        "text_transcription",
        "speaker",
        "speaker_id",
        "language",
    }
    results = {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        fields = defaultdict(Counter)
        files, rows = 0, 0
        for path in sorted(directory.rglob("*.parquet")):
            pf = pq.ParquetFile(path)
            columns = sorted(selected.intersection(pf.schema_arrow.names))
            files += 1
            rows += pf.metadata.num_rows
            for batch in pf.iter_batches(columns=columns, batch_size=65536):
                for name in columns:
                    col = batch.column(name)
                    if not (pa.types.is_string(col.type) or pa.types.is_large_string(col.type)):
                        continue
                    fields[name]["null"] += col.null_count
                    empty = pc.equal(pc.utf8_trim_whitespace(col), "")
                    fields[name]["empty"] += pc.sum(pc.cast(empty, pa.int64())).as_py() or 0
                    fields[name]["rows"] += len(col)
        if files:
            results[directory.name] = {
                "files": files,
                "rows": rows,
                "fields": {k: dict(v) for k, v in fields.items()},
            }
            print(f"Scalar scan: {directory.name}, {rows} rows", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    return results
