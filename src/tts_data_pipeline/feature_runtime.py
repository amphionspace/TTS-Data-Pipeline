"""Shared pinned-selection planning and checkpoint utilities for audio features."""

import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow.compute as pc

from .feature_contract import validate_feature_coverage
from .schema import digest
from .timestamps import BEIJING


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-writer temp name: concurrent writers (e.g. reuse and encode modes on two
    # machines) must never open the same temp file.
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temp.open("wb") as stream:
        stream.write(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def event(work, phase, **fields):
    value = {"at": datetime.now(BEIJING).isoformat(), "phase": phase, **fields}
    write_json(Path(work) / "status.json", value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def open_source(root, source):
    return lance.dataset(Path(root) / source["table_path"]).checkout_version(
        (source["branch"], source["lance_version"])
    )


def ordered_hash(ids):
    return hashlib.sha256(b"".join(i.encode("ascii") for i in ids)).hexdigest()


def sorted_set_hash(ids):
    ids.sort()
    if len(ids) > 1 and np.any(ids[1:] == ids[:-1]):
        raise ValueError("Duplicate target identity")
    h = hashlib.sha256()
    for sid in ids:
        sid = bytes(sid)
        h.update(b'["sample","' + sid + b'","' + sid + b'"]\n')
    return h.hexdigest()


def plan_dataset(args):
    root, source, task_size, work = args
    ds = open_source(root, source)
    tasks = []
    all_ids = []
    for fragment in ds.get_fragments():
        table = ds.scanner(
            fragments=[fragment],
            columns=["sample_id"],
            filter="selection_reason = 0",
            scan_in_order=True,
        ).to_table()
        ids = table["sample_id"].to_pylist()
        all_ids.append(np.asarray(ids, dtype="S64"))
        for offset in range(0, len(ids), task_size):
            selected = ids[offset : offset + task_size]
            task = {
                "dataset": source["dataset_id"],
                "fragment": fragment.fragment_id,
                "offset": offset,
                "rows": len(selected),
                "ordered_ids_sha256": ordered_hash(selected),
            }
            task["id"] = digest(task)
            tasks.append(task)
    ids = np.concatenate(all_ids)
    if len(ids) != source["selected_rows"]:
        raise ValueError("Selection selected count changed")
    checksum = sorted_set_hash(ids)
    result = {
        "source": source,
        "target_count": len(ids),
        "target_set_sha256": checksum,
        "tasks": tasks,
    }
    write_json(Path(work) / f"{source['dataset_id']}.targets.json", result)
    return result


_LOST_LOCK = threading.Lock()
_LOST = {}


def lost_file(d, task, missing):
    """Account for a checkpointed data file the filesystem lost, and make its redo possible.

    On ACP nodes the quarkfs client can lose a successful rename(2) minutes after the writer's
    own readback, leaving the task's data file absent. Such a task never durably finished.
    Steps write fragments with mode="create", which refuses an existing dataset, and publish
    an Overwrite of every checkpoint, so a stale commit in the unpublished stage is discarded
    (data files stay). Everything else is refused: an unowned or unlistable stage, or more lost
    files than sporadic loss explains, which means a missing mount, not lost renames.
    """
    stage = Path(d["stage"])
    destination = stage / "features.lance"
    if not stage.name.endswith(".incomplete") or not (stage / "execution.json").exists():
        raise ValueError(f"Checkpointed file missing outside an owned stage: {missing}")
    os.listdir(destination / "data")
    with _LOST_LOCK:
        lost = _LOST.setdefault(str(stage), set())
        lost.add(task["id"])
        if len(lost) > max(8, len(d["tasks"]) // 100):
            raise ValueError(f"Too many lost checkpointed files in {stage}: {len(lost)}")
        # Codec reuse and encode modes may run at once under separate locks; a discard
        # there could race the other mode's publish, so they must not share a lossy stage.
        removed = [
            n
            for n in ("_versions", "_refs", "_transactions", "_indices")
            if (destination / n).exists()
        ]
        for name in removed:
            shutil.rmtree(destination / name)
        if (destination / "_versions").exists() or (destination / "_refs").exists():
            raise ValueError(f"Could not discard the stale commit in {stage}")
        record = dict(
            at=datetime.now(BEIJING).isoformat(),
            event="lost_checkpoint_file",
            task=task["id"],
            missing=str(missing),
            discarded=removed,
        )
        print(json.dumps(record), flush=True)
        with (Path(d["state"]) / "lost_files.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")


def verify_checkpoint(d, task):
    """The task's checkpoint if every file it names is intact; None means redo the task.

    A missing reference plan is regenerated by the redo. A missing data file is a lost
    rename (see `lost_file`); a present file with other content still raises.
    """
    path = Path(d["state"]) / "checkpoints" / f"{task['id']}.json"
    if not path.exists():
        return None
    c = json.loads(path.read_text())
    if (
        c["task"] != task
        or c.get("input_rows", c["rows"]) != task["rows"]
        or not c.get("payloads_validated")
    ):
        raise ValueError("Checkpoint task changed")
    reference = Path(d["state"]) / "references" / f"{task['id']}.arrow"
    if "reference_plan_sha256" in c and not reference.exists():
        return None
    files = [Path(d["stage"]) / "features.lance/data" / f["name"] for f in c["files"]]
    missing = next((f for f in files if not f.exists()), None)
    if missing is not None:
        lost_file(d, task, missing)
        return None
    if "reference_plan_sha256" in c and file_hash(reference) != c["reference_plan_sha256"]:
        raise ValueError("Reference plan corrupted")
    for f, record in zip(files, c["files"], strict=True):
        if file_hash(f) != record["sha256"]:
            raise ValueError("Checkpoint file corrupted")
    return c


def published_manifest(p, d):
    final = Path(d["final"])
    manifest = json.loads((final / "manifest.json").read_text())
    validate_feature_coverage(manifest)
    ds = lance.dataset(final / "features.lance", version=manifest["lance_version"])
    if (
        manifest["profile_id"] != p["profile_id"]
        or manifest["run_id"] != d["run_id"]
        or manifest["selection"]["target_set_sha256"] != d["target_set_sha256"]
        or manifest["selection"]["manifest_sha256"] != p["selection_sha256"]
        or ds.count_rows() != d["target_count"]
    ):
        raise ValueError("Published output differs from plan")
    return manifest


def align(table, ids):
    if table.num_rows != len(ids) or pc.count_distinct(table["target_id"]).as_py() != len(ids):
        raise ValueError("Missing or duplicate feature targets")
    if table["target_id"].equals(ids):
        return table
    positions = pc.index_in(ids, value_set=table["target_id"])
    if positions.null_count:
        raise ValueError("Feature target sets differ")
    return table.take(positions)


def source_table(item):
    return lance.dataset(item["table_path"], version=item["lance_version"])


def read_aligned(ds, task, ids, columns=None):
    table = ds.scanner(
        columns=columns,
        offset=task["offset"],
        limit=task["rows"],
        scan_in_order=True,
        batch_readahead=1,
        fragment_readahead=1,
    ).to_table()
    # Positional reads are only an optimization. The ID join verifies every row.
    if table.num_rows == len(ids) and not pc.index_in(ids, value_set=table["target_id"]).null_count:
        return table
    values = ids.to_pylist()
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values):
        raise ValueError("Invalid target ID")
    return ds.to_table(
        columns=columns, filter="target_id IN (" + ",".join(f"'{v}'" for v in values) + ")"
    )
