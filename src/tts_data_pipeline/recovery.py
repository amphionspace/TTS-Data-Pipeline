"""Recover feature-run files that the shared filesystem lost on a rename.

On ACP nodes the quarkfs client can lose a successful rename(2) minutes after the writer read the
file back, leaving the full source under its temporary name, or fail it with EINVAL. A step that
finds a checkpointed file missing redoes the task (`feature_runtime.lost_file`); this module saves
that recompute and protects publishes by restoring the lost name from a byte-identical source.
It runs on a host whose renames are reliable, never overwrites, and never deletes:

- a checkpointed data file `data/<name>.lance` from a `.tmp*` sibling (or a quarantined copy)
  of the recorded size and sha256;
- a checkpoint `<task>.json` from its `write_json` temp when that temp is a checkpoint
  `verify_checkpoint` would accept;
- a speaker reference plan `references/<task>.arrow` from `<task>.tmp` of the recorded sha256.

Candidates younger than `min_age` are left for the next scan: a live writer may still own them.
Tasks the pipeline already chose to redo (`state/lost_files.jsonl`) are never restored. Anything
it cannot restore, and published or committed tables that reference a missing file, are alerts.
"""

import json
import os
import time
from pathlib import Path

import lance

from .feature_runtime import file_hash

DATA_KEYS = {"stage", "state", "final", "tasks"}


def plan_datasets(work):
    """(step, plan, dataset) for every step plan of a feature work root."""
    steps = json.loads((Path(work) / "pipeline.json").read_text())["steps"]
    for step, path in steps.items():
        plan_path = Path(path) / "plan.json"
        if not plan_path.exists():
            continue
        plan = json.loads(plan_path.read_text())
        for d in plan.get("datasets", []):
            if DATA_KEYS <= d.keys():
                yield step, plan, d


def dataset_name(d):
    return d.get("source", {}).get("dataset_id") or Path(d["final"]).name


def profile_id(plan, d):
    """Merge plans carry the profile per dataset, the other steps per plan."""
    return d.get("profile_id", plan.get("profile_id"))


def redone_tasks(d):
    path = Path(d["state"]) / "lost_files.jsonl"
    if not path.exists():
        return set()
    return {json.loads(line)["task"] for line in path.read_text().splitlines() if line}


def age(path):
    return time.time() - path.stat().st_mtime


class Recovery:
    """One scan over a work root; `events` collects (kind, message) for the caller to report."""

    def __init__(self, work, *, apply=False, min_age=120, quarantine=None):
        self.work, self.apply, self.min_age = Path(work), apply, min_age
        self.quarantine = Path(quarantine) if quarantine else None
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))

    def restore(self, source, target, expected_sha256, where):
        if not self.apply:
            self.event("DRY-RUN", f"would restore {source.name} -> {where}")
        elif target.exists():
            self.event("SKIP", f"target appeared: {where}")
        else:
            os.rename(source, target)
            ok = target.exists() and file_hash(target) == expected_sha256
            self.event("RESTORED" if ok else "ALERT", f"{source.name} -> {where}")

    # ------------------------------------------------------------------ data files
    def candidates(self, d, data, size):
        """Aged sources of `size` bytes: .tmp siblings, then quarantined copies."""
        found = []
        dirs = [data] + ([self.quarantine / Path(d["stage"]).name] if self.quarantine else [])
        for directory in dirs:
            if not directory.exists():
                continue
            for entry in os.listdir(directory):
                path = directory / entry
                try:
                    if entry.startswith(".tmp") and path.stat().st_size == size:
                        found.append((path, age(path)))
                except FileNotFoundError:
                    continue
        return found

    def data_files(self, step, d, data, checkpoints):
        disk, redone = set(os.listdir(data)), redone_tasks(d)
        for cp in checkpoints.glob("*.json"):
            if cp.stem in redone:
                continue
            try:
                files = json.loads(cp.read_text())["files"]
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            for f in files:
                target = data / f["name"]
                # A listing races live writers: confirm absence with a direct stat.
                if f["name"] in disk or target.exists():
                    continue
                where = f"{step} {dataset_name(d)} checkpoint={cp.name} file={f['name']}"
                found = self.candidates(d, data, f["bytes"])
                if any(a <= self.min_age for _, a in found):
                    continue  # A fresh copy may still belong to a writer: next scan.
                match = next((p for p, _ in found if file_hash(p) == f["sha256"]), None)
                if match is None:
                    self.event("ALERT", f"lost without a matching source: {where}")
                else:
                    self.restore(match, target, f["sha256"], where)

    # ------------------------------------------------------------------ checkpoints
    def valid_checkpoint(self, path, task, profile, data):
        """What verify_checkpoint would accept: same task and profile, every file intact."""
        try:
            c = json.loads(path.read_text())
            return (
                c["task"] == task
                and c["profile_id"] == profile
                and c.get("payloads_validated") is True
                and all(
                    (data / f["name"]).exists() and file_hash(data / f["name"]) == f["sha256"]
                    for f in c["files"]
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def checkpoints(self, step, plan, d, data, checkpoints):
        redone = redone_tasks(d)
        tasks = {t["id"]: t for t in d["tasks"] if t["id"] not in redone}
        for temp in checkpoints.glob("*.json.*.tmp"):
            target = checkpoints / (temp.name.split(".json.")[0] + ".json")
            try:
                # A temp whose target exists is a superseded or finished write, not a loss.
                if age(temp) <= self.min_age or target.exists():
                    continue
            except FileNotFoundError:
                continue
            where = f"{step} {dataset_name(d)} checkpoint={target.name}"
            task = tasks.get(target.stem)
            if task is None or not self.valid_checkpoint(temp, task, profile_id(plan, d), data):
                self.event("ALERT", f"lost checkpoint without a valid temp: {where}")
            else:
                self.restore(temp, target, file_hash(temp), where)

    # ------------------------------------------------------------------ reference plans
    def references(self, step, d, checkpoints):
        references = Path(d["state"]) / "references"
        if not references.exists():
            return
        redone = redone_tasks(d)
        for cp in checkpoints.glob("*.json"):
            target = references / f"{cp.stem}.arrow"
            if cp.stem in redone or target.exists():
                continue
            try:
                expected = json.loads(cp.read_text()).get("reference_plan_sha256")
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if expected is None:
                continue
            temp = references / f"{cp.stem}.tmp"
            where = f"{step} {dataset_name(d)} reference={target.name}"
            try:
                if age(temp) <= self.min_age:
                    continue
            except FileNotFoundError:
                self.event("ALERT", f"lost reference plan without a temp: {where}")
                continue
            if file_hash(temp) != expected:
                self.event("ALERT", f"lost reference plan, temp differs: {where}")
            else:
                self.restore(temp, target, expected, where)

    # ------------------------------------------------------------------ quarantine
    def quarantine_temps(self, d, data):
        """Hard-link aged .tmp files out of the stage: publish cleanup unlinks every .tmp."""
        if not self.quarantine or not self.apply:
            return
        target = self.quarantine / Path(d["stage"]).name
        for entry in os.listdir(data):
            try:
                if not entry.startswith(".tmp") or (target / entry).exists():
                    continue
                if age(data / entry) <= self.min_age:
                    continue
                target.mkdir(parents=True, exist_ok=True)
                os.link(data / entry, target / entry)
            except FileNotFoundError:
                continue

    # ------------------------------------------------------------------ table checks
    def missing_referenced(self, step, d):
        """Alert on files a stage's latest commit or a final's manifest version lacks."""
        stage, final = Path(d["stage"]), Path(d["final"])
        tables = []
        if (stage / "features.lance/_versions").exists():
            tables.append(("stage", stage / "features.lance", None))
        if (final / "manifest.json").exists():
            m = json.loads((final / "manifest.json").read_text())
            tables.append(("final", final / "features.lance", m["lance_version"]))
            subset = m.get("selection", {}).get("table")
            if subset:
                tables.append(("final targets", final / "targets.lance", subset["lance_version"]))
        for kind, path, version in tables:
            try:
                ds = lance.dataset(path, version=version)
                disk = set(os.listdir(path / "data"))
                missing = [
                    f.path
                    for fragment in ds.get_fragments()
                    for f in fragment.metadata.files
                    if f.path not in disk and not (path / "data" / f.path).exists()
                ]
            except Exception as error:
                # A stage mid-commit may be briefly unreadable; a final must always open.
                if kind != "stage":
                    self.event("ALERT", f"cannot open {kind} {path}: {error}")
                continue
            for name in missing:
                self.event("ALERT", f"{kind} references a missing file: {step} {path} {name}")

    def scan(self):
        for step, plan, d in plan_datasets(self.work):
            self.missing_referenced(step, d)
            data = Path(d["stage"]) / "features.lance" / "data"
            checkpoints = Path(d["state"]) / "checkpoints"
            if data.exists():
                self.quarantine_temps(d, data)
            if not data.exists() or not checkpoints.exists():
                continue
            self.data_files(step, d, data, checkpoints)
            self.checkpoints(step, plan, d, data, checkpoints)
            self.references(step, d, checkpoints)
        return self.events
