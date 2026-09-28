"""Shared bounded Parquet reader; source semantics stay in each adapter."""

from pathlib import Path

import pyarrow.parquet as pq

from ..source_exclusions import verify_excluded_row


def source_rows(root: Path, files: list[Path] | None = None, excluded_records=()):
    pending = {(item["path"], item["row"]): item for item in excluded_records}
    for path in sorted(files if files is not None else root.rglob("*.parquet")):
        offset = 0
        for batch in pq.ParquetFile(path).iter_batches(batch_size=16):
            for row in batch.to_pylist():
                relative, index = str(path.relative_to(root)), offset
                offset += 1
                item = pending.pop((relative, index), None)
                if item is not None:
                    verify_excluded_row(row, item)
                    continue
                yield relative, index, row
    if pending:
        raise ValueError("Excluded record not found in selected input")
