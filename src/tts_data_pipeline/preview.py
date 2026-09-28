"""Small real-data contract check. This is not the full production converter."""

import itertools
import json
from pathlib import Path

import lance

from .adapters import ADAPTERS
from .convert import decode_check
from .schema import validate_record
from .writer import write_records


def preview(
    dataset: str,
    root: Path,
    snapshot: str,
    output: Path,
    limit: int,
    config: str | None = None,
    files: list[Path] | None = None,
) -> dict:
    if not 1 <= limit <= 32:
        raise ValueError("Preview limit must be in [1, 32]")
    root = root.resolve(strict=True)
    output = output.resolve()
    if output == root or root in output.parents:
        raise ValueError("Preview output must be outside the raw corpus")
    adapter = ADAPTERS[dataset]
    selected = files if files is not None else adapter.input_files(root, config)
    if hasattr(adapter, "iter_preview_records"):
        source = adapter.iter_preview_records(root, snapshot, files=selected, limit=limit)
    else:
        source = adapter.iter_records(root, snapshot, files=selected)
    try:
        rows = list(itertools.islice(source, limit))
    finally:
        source.close()
    if not rows:
        raise ValueError("No input records")
    if len({r["sample_id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate source identity")
    output.mkdir(parents=True, exist_ok=False)
    table_path = output / "preview.lance"
    write_records(rows, table_path)
    restored = lance.dataset(table_path).to_table().to_pylist()
    for row in restored:
        validate_record(row)
        decode_check(row)
    result = {
        "dataset": dataset,
        "source_snapshot": snapshot,
        "rows": len(rows),
        "scope": "bounded prefix preview; not a verified full-source snapshot",
        "fully_decoded_audio": len(rows),
        "source_files": [str(p.relative_to(root)) for p in selected],
        "audio_bytes_preserved": all(
            a["audio"]["bytes"] == b["audio"]["bytes"] for a, b in zip(rows, restored, strict=True)
        ),
        "sample_rates": sorted({r["sample_rate"] for r in rows}),
        "status": "Lance roundtrip and record validation passed",
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
