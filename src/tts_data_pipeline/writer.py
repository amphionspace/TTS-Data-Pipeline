"""Native Lance storage; workers write fragments, the coordinator commits one table."""

import importlib.metadata
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path

import lance
import pyarrow as pa
import soundfile as sf
from lance.file import LanceFileReader
from lance.fragment import FragmentMetadata, write_fragments

from .manifests import read_base_manifest
from .schema import base_schema, validate_record

STORAGE_FORMAT = "lance"
STORAGE_VERSION = "2.2"
TABLE_PATH = "samples.lance"


def remove_uncommitted_data_files(directory, referenced, *, audit_path=None):
    """Remove known writer leftovers after all writers stop and references are audited.

    The caller owns the exclusive conversion lock. Published-table maintenance
    must include every retained snapshot, not just the current manifest.
    """
    referenced = {Path(path).name for path in referenced}
    removed = []

    def record(entry, action):
        event = dict(entry, action=action, at=datetime.now(timezone.utc).isoformat())
        if audit_path is not None:
            Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
            with Path(audit_path).open("a") as stream:
                stream.write(json.dumps(event) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        print(f"Cleanup: {json.dumps(event)}", flush=True)

    for path in sorted(Path(directory).iterdir()):
        if (
            not path.is_symlink()
            and path.is_file()
            and path.name not in referenced
            and (path.suffix == ".lance" or path.name.startswith(".tmp"))
        ):
            entry = {"name": path.name, "bytes": path.stat().st_size}
            # Persist intent before unlink; a crash cannot erase the deletion's evidence.
            record(entry, "planned")
            path.unlink()
            record(entry, "removed")
            removed.append(entry)
    return removed


def dependencies():
    versions = {
        name: importlib.metadata.version(name)
        for name in ("pylance", "pyarrow", "soundfile", "numpy", "py7zr", "PyYAML")
    }
    versions.update(python=platform.python_version(), libsndfile=sf.__libsndfile_version__)
    return versions


def write_batches(batches, path, shard_bytes=1024**3):
    failure = []

    def checked():
        try:
            yield from batches
        except Exception as exc:
            failure.append(exc)
            raise

    try:
        return write_fragments(
            pa.RecordBatchReader.from_batches(base_schema(), checked()),
            path,
            schema=base_schema(),
            mode="create",
            data_storage_version=STORAGE_VERSION,
            max_bytes_per_file=shard_bytes,
            max_rows_per_file=2**31 - 1,
        )
    except Exception:
        # Arrow's C stream wraps Python validation failures as OSError.
        if failure:
            raise failure[0]
        raise


def commit_fragments(path, fragments, *, index=True):
    metadata = [FragmentMetadata.from_json(json.dumps(f)) for f in fragments]
    try:
        previous = lance.dataset(path)
    except ValueError:
        previous = None
    # Retrying finalization may find an already committed unpublished table.
    ds = lance.LanceDataset.commit(
        path,
        lance.LanceOperation.Overwrite(base_schema(), metadata),
        read_version=0 if previous is None else previous.version,
    )
    if index:
        ds.create_scalar_index("sample_id", "BTREE")
    return ds


def file_batches(path, columns=None):
    # Only used for new base fragments: one self-contained file per fragment.
    return (
        LanceFileReader(str(path), columns=columns)
        .read_all(batch_size=64, batch_readahead=1)
        .to_batches()
    )


def release_dataset(release):
    release = Path(release)
    manifest = read_base_manifest(release)
    if manifest.get("status") != "complete" or manifest.get("storage_format") != STORAGE_FORMAT:
        raise ValueError("Not a published Lance release")
    version = manifest.get("lance_version")
    if release.name.endswith(".incomplete") or type(version) is not int or version <= 0:
        raise ValueError("Published release requires an explicit positive snapshot version")
    if manifest.get("table_path") != TABLE_PATH:
        raise ValueError("Unexpected base table path")
    return lance.dataset(release / TABLE_PATH, version=version)


def write_records(rows: list[dict], path: Path) -> None:
    for row in rows:
        validate_record(row)
    fragments = write_batches(pa.Table.from_pylist(rows, schema=base_schema()).to_batches(), path)
    commit_fragments(path, [f.to_json() for f in fragments])
