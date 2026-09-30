"""Shared pinned-selection planning and checkpoint utilities for audio features."""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import lance
import numpy as np

from .feature_contract import validate_feature_coverage
from .schema import digest
from .timestamps import BEIJING


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
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


def verify_checkpoint(d, task):
    path = Path(d["state"]) / "checkpoints" / f"{task['id']}.json"
    if not path.exists():
        return None
    c = json.loads(path.read_text())
    if c["task"] != task or c["rows"] != task["rows"] or not c.get("payloads_validated"):
        raise ValueError("Checkpoint task changed")
    for f in c["files"]:
        if file_hash(Path(d["stage"]) / "features.lance/data" / f["name"]) != f["sha256"]:
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
