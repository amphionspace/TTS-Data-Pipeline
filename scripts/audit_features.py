#!/usr/bin/env python
"""Read-only post-run audit of a feature work root's published finals (codec/text/speaker/merged).

For every dataset of every step plan whose final exists:
  - manifest loads, validate_feature_coverage passes, the table opens at manifest["lance_version"],
    the run_id tag points to that version, published_manifest-style identity checks pass;
  - every data file the version references exists with the recorded size, every index directory
    exists; nothing unreferenced, no .tmp, no stray entries in the final directory;
  - every column of every row is read (one scanner per fragment, a shared thread pool);
  - the target_id order per fragment is hashed and compared with the plan's pinned tasks
    (falls back to a sorted-set hash if the order differs);
  - every scalar index is queried once (and the plan must use it);
  - manifest inputs: every referenced manifest exists and has the recorded sha256/version;
  - merged: targets.lance opens at its pinned version and equals the merged target_id stream,
    which equals the text stream minus the codec/speaker failures; sources match the sibling plans.
Across datasets: codec/text/speaker rows == selection selected_rows; merged accounts for all.
Work root: no hold, no leftover .incomplete stage per plan dataset.

Writes nothing but the JSON report (--out). Never opens any file for writing elsewhere.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa

DEFAULT_REPO = Path(__file__).resolve().parents[1] / "src"
KIND_OF_STEP = {
    "codec": "codec",
    "text": "text",
    "speaker-early": "speaker",
    "speaker-late": "speaker",
    "merged-early": "merged",
    "merged-late": "merged",
}
MANIFEST_KIND = {
    "codec": "codec",
    "text": "text",
    "speaker": "speaker_embedding",
    "merged": "merged",
}
EXPECTED_INDICES = {
    "codec": {"target_id", "feature_key"},
    "speaker": {"target_id", "feature_key"},
    "text": {"target_id"},
    "merged": {"target_id"},
}
LANCE_DIRS = {"data", "_indices", "_versions", "_refs", "_transactions", "_deletions"}


def log(message):
    print(f"{datetime.now().astimezone():%H:%M:%S %Z} {message}", file=sys.stderr, flush=True)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def sorted_set_hash(ids):
    """feature_runtime.sorted_set_hash, without the import (ids: numpy S64)."""
    ids = np.sort(ids)
    if len(ids) > 1 and np.any(ids[1:] == ids[:-1]):
        raise ValueError("Duplicate target identity")
    h = hashlib.sha256()
    for sid in ids:
        sid = bytes(sid)
        h.update(b'["sample","' + sid + b'","' + sid + b'"]\n')
    return h.hexdigest()


def member(ids, sorted_unique):
    """np.isin for a sorted, unique haystack without re-sorting it for every batch."""
    if not len(sorted_unique):
        return np.zeros(len(ids), dtype=bool)
    pos = np.searchsorted(sorted_unique, ids)
    pos[pos == len(sorted_unique)] = 0
    return sorted_unique[pos] == ids


def ids_array(column):
    return column.to_numpy(zero_copy_only=False).astype("S64")


class Final:
    """Audit state of one dataset's final."""

    def __init__(self, step, kind, plan, d):
        self.step, self.kind, self.plan, self.d = step, kind, plan, d
        self.dataset = d["source"]["dataset_id"]
        self.final = Path(d["final"])
        self.problems, self.warnings, self.info = [], [], {}
        self.manifest = None
        self.ds = None
        self.fragments = []
        self.scan = dict(fragments=0, rows=0, bytes=0, seconds=0.0)
        self.scan_results = {}

    def problem(self, text):
        self.problems.append(text)

    def warn(self, text):
        self.warnings.append(text)

    @property
    def label(self):
        return f"{self.kind}/{self.dataset}"

    def report(self):
        return dict(
            step=self.step,
            kind=self.kind,
            dataset=self.dataset,
            final=str(self.final),
            run_id=self.d.get("run_id"),
            published=self.manifest is not None,
            lance_version=self.manifest and self.manifest.get("lance_version"),
            rows=self.manifest and self.manifest.get("rows"),
            scan=self.scan,
            info=self.info,
            problems=self.problems,
            warnings=self.warnings,
        )


class Audit:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work).resolve()
        self.pipeline = json.loads((self.work / "pipeline.json").read_text())
        self.pool = ThreadPoolExecutor(max_workers=args.threads)
        self.finals = []
        self.global_problems, self.global_warnings = [], []
        self.progress = dict(fragments=0, total_fragments=0, rows=0, bytes=0)
        self.lock = threading.Lock()
        sys.path.insert(0, str(Path(args.repo)))
        from tts_data_pipeline.contract import schema_description
        from tts_data_pipeline.feature_contract import validate_feature_coverage
        from tts_data_pipeline.schema import digest

        self.validate_feature_coverage = validate_feature_coverage
        self.schema_sha = lambda schema: digest(schema_description(schema))

    # ------------------------------------------------------------------ plans
    def load_plans(self):
        wanted_kinds = (
            set(self.args.kinds.split(",")) if self.args.kinds else set(KIND_OF_STEP.values())
        )
        wanted = set(self.args.datasets.split(",")) if self.args.datasets else None
        self.plans = {}
        for step, path in self.pipeline["steps"].items():
            kind = KIND_OF_STEP[step]
            plan_path = Path(path) / "plan.json"
            if not plan_path.exists():
                self.plans[step] = None
                continue
            plan = json.loads(plan_path.read_text())
            self.plans[step] = plan
            if kind not in wanted_kinds:
                continue
            for d in plan["datasets"]:
                if wanted and d["source"]["dataset_id"] not in wanted:
                    continue
                self.finals.append(Final(step, kind, plan, d))
        selection = Path(self.pipeline["selection"])
        self.selection = json.loads(selection.read_text())
        if file_sha256(selection) != self.pipeline["selection_sha256"]:
            self.global_problems.append("selection manifest sha256 differs from pipeline.json")
        self.selected_rows = {
            o["dataset_id"]: o["selected_rows"] for o in self.selection["outputs"]
        }

    # ------------------------------------------------------------- metadata
    def check_metadata(self, f):
        d, p = f.d, f.plan
        stage_exists = Path(d["stage"]).exists()
        manifest_path = f.final / "manifest.json"
        if not manifest_path.exists():
            f.info["status"] = "unpublished"
            f.info["stage_exists"] = stage_exists
            if self.args.require_all:
                f.problem("final not published")
            return False
        if stage_exists:
            f.problem(f"stage still exists beside the published final: {d['stage']}")
        m = json.loads(manifest_path.read_text())
        f.manifest = m
        try:
            self.validate_feature_coverage(m)
        except Exception as e:  # noqa: BLE001
            f.problem(f"validate_feature_coverage: {e}")
        # Identity (published_manifest / merge_features.published semantics)
        checks = {
            "status": m.get("status") == "complete",
            "kind": m.get("kind") == MANIFEST_KIND[f.kind],
            "dataset_id": m.get("dataset_id") == f.dataset,
            "run_id": m.get("run_id") == d["run_id"],
            "selection_manifest_sha256": m["selection"]["manifest_sha256"] == p["selection_sha256"],
            "table_path": m["table_path"].endswith(f"{f.final.name}/features.lance"),
        }
        if f.kind == "merged":
            checks.update(
                profile_id=m["profile_id"] == d["profile_id"],
                rows=m["rows"] == d["output_count"],
                excluded_input_rows=m["excluded_targets"]["input_rows"] == d["target_count"],
                excluded_total=m["excluded_targets"]["total"] == d["excluded_count"],
                excluded_retained=m["excluded_targets"]["retained"] == m["rows"],
                coverage_ok=m["coverage"]["ok"] == m["rows"],
            )
            if m["selection"]["mode"] == "subset":
                checks["subset_rows"] = m["selection"]["table"]["rows"] == m["rows"]
            else:
                checks["target_set"] = m["selection"]["target_set_sha256"] == d["target_set_sha256"]
        else:
            checks.update(
                profile_id=m["profile_id"] == p["profile_id"],
                rows=m["rows"] == d["target_count"],
                target_set=m["selection"]["target_set_sha256"] == d["target_set_sha256"],
                coverage_sum=m["coverage"]["ok"] + m["coverage"]["failed"] == m["rows"],
            )
            if f.kind == "text":
                checks["coverage_ok"] = m["coverage"]["ok"] == m["rows"]
        for name, ok in checks.items():
            if not ok:
                f.problem(f"identity check failed: {name}")
        # Open at the manifest version; tag
        try:
            ds = lance.dataset(str(f.final / "features.lance"), version=m["lance_version"])
        except Exception as e:  # noqa: BLE001
            f.problem(f"cannot open features.lance at version {m['lance_version']}: {e}")
            return False
        f.ds = ds
        n = ds.count_rows()
        if n != m["rows"]:
            f.problem(f"count_rows {n} != manifest rows {m['rows']}")
        latest = lance.dataset(str(f.final / "features.lance")).version
        f.info["latest_version"] = latest
        if latest != m["lance_version"]:
            f.warn(f"latest version {latest} != manifest version {m['lance_version']}")
        try:
            tags = ds.tags.list()
            tag = tags.get(d["run_id"])
            if tag is None:
                f.problem("run_id tag missing")
            elif tag["version"] != m["lance_version"]:
                f.problem(f"tag version {tag['version']} != manifest version {m['lance_version']}")
            extra = sorted(set(tags) - {d["run_id"]})
            if extra:
                f.warn(f"extra tags: {extra}")
        except Exception as e:  # noqa: BLE001
            f.problem(f"tags: {e}")
        if self.schema_sha(ds.schema) != m["schema_sha256"]:
            f.problem("schema_sha256 differs from the table schema")
        return True

    # ---------------------------------------------------------------- files
    def check_files(self, f):
        ds, m, d = f.ds, f.manifest, f.d
        table = f.final / "features.lance"
        fragments = ds.get_fragments()
        f.fragments = fragments
        referenced_now = {}
        for fr in fragments:
            meta = fr.metadata
            for df in meta.files:
                referenced_now[df.path] = df.file_size_bytes
            if meta.deletion_file is not None:
                f.warn(f"fragment {meta.id} has a deletion file")
        f.info["fragments"] = len(fragments)
        f.info["data_files"] = len(referenced_now)
        # Every referenced file exists with the recorded size (parallel stats).
        uid = os.getuid()
        data_dir = table / "data"

        def stat_chunk(names):
            missing, wrong, foreign = [], [], 0
            for name in names:
                try:
                    st = os.stat(data_dir / name)
                except FileNotFoundError:
                    missing.append(name)
                    continue
                expected = referenced_now[name]
                if expected and st.st_size != expected:
                    wrong.append((name, st.st_size, expected))
                if st.st_uid != uid:
                    foreign += 1
            return missing, wrong, foreign

        names = sorted(referenced_now)
        chunks = [names[i : i + 256] for i in range(0, len(names), 256)]
        missing, wrong, foreign = [], [], 0
        for a, b, c in self.pool.map(stat_chunk, chunks):
            missing += a
            wrong += b
            foreign += c
        if missing:
            f.problem(f"{len(missing)} referenced data files missing, e.g. {missing[:3]}")
        if wrong:
            f.problem(f"{len(wrong)} data files with wrong size, e.g. {wrong[:3]}")
        if foreign:
            f.warn(f"{foreign} data files not owned by uid {uid}")
        # Unreferenced / temp / stale files in data/
        entries = os.listdir(data_dir)
        f.info["data_dir_entries"] = len(entries)
        referenced_any = set()
        index_any = set()
        for v in ds.versions():
            old = ds.checkout_version(v["version"])
            for fr in old.get_fragments():
                referenced_any.update(df.path for df in fr.metadata.files)
            index_any.update(i["uuid"] for i in old.list_indices())
        tmp = [e for e in entries if e.startswith(".tmp") or not e.endswith(".lance")]
        unreferenced = [e for e in entries if e not in referenced_any and e not in tmp]
        stale = [e for e in entries if e in referenced_any and e not in referenced_now]
        if tmp:
            f.problem(f"{len(tmp)} temp/odd entries in data/, e.g. {tmp[:3]}")
        if unreferenced:
            f.problem(
                f"{len(unreferenced)} data files referenced by no version, e.g. {unreferenced[:3]}"
            )
        if stale:
            f.warn(f"{len(stale)} data files referenced only by older versions")
        # Indices
        indices = ds.list_indices()
        f.info["indices"] = [
            dict(name=i["name"], fields=list(i["fields"]), type=i["type"]) for i in indices
        ]
        have = {i["fields"][0] for i in indices if len(i["fields"]) == 1}
        for col in EXPECTED_INDICES[f.kind] - have:
            f.problem(f"missing scalar index on {col}")
        for i in indices:
            idir = table / "_indices" / i["uuid"]
            if not idir.is_dir() or not any(p.stat().st_size for p in idir.iterdir()):
                f.problem(f"index {i['name']} directory missing or empty: {idir}")
            covered = len(i["fragment_ids"]) if i.get("fragment_ids") is not None else None
            if covered is not None and covered != len(fragments):
                f.warn(f"index {i['name']} covers {covered}/{len(fragments)} fragments")
        index_dirs = set(os.listdir(table / "_indices")) if (table / "_indices").exists() else set()
        current = {i["uuid"] for i in indices}
        orphan_idx = sorted(index_dirs - index_any)
        old_idx = sorted((index_dirs & index_any) - current)
        if orphan_idx:
            f.problem(f"{len(orphan_idx)} index directories referenced by no version")
        if old_idx:
            f.warn(f"{len(old_idx)} index directories referenced only by older versions")
        # Final directory and table directory hygiene
        allowed = {"features.lance", "manifest.json"}
        if (m.get("cleanup") or {}).get("audit_path"):
            allowed.add(m["cleanup"]["audit_path"])
        if f.kind == "merged" and m["selection"]["mode"] == "subset":
            allowed.add("targets.lance")
        stray = sorted(set(os.listdir(f.final)) - allowed)
        if stray:
            f.problem(f"stray entries in final: {stray}")
        odd = sorted(set(os.listdir(table)) - LANCE_DIRS)
        if odd:
            f.warn(f"unexpected entries in features.lance: {odd}")
        refs = table / "_refs" / "tags" / f"{d['run_id']}.json"
        if not refs.exists():
            f.warn(f"tag file missing on disk: {refs}")
        version_file = table / "_versions" / f"{(1 << 64) - 1 - m['lance_version']}.manifest"
        if not version_file.exists():
            f.warn(f"version manifest file not at the expected name: {version_file.name}")

    # --------------------------------------------------------------- inputs
    def check_inputs(self, f):
        m = f.manifest
        root = Path(m["input_root"])
        for item in m.get("inputs", []):
            path = Path(item["manifest_path"])
            if not path.is_absolute():
                path = root / path
            if not path.exists():
                f.problem(f"input manifest missing: {item.get('alias')} {path}")
                continue
            if file_sha256(path) != item["manifest_sha256"]:
                f.problem(f"input manifest sha256 differs: {item.get('alias')} {path}")
                continue
            if item.get("artifact_kind") == "feature":
                src = json.loads(path.read_text())
                if src.get("lance_version") != item.get("lance_version") or src.get(
                    "run_id"
                ) != item.get("run_id"):
                    f.problem(f"input {item.get('alias')} version/run_id differs from its manifest")
                if src.get("status") != "complete":
                    f.problem(f"input {item.get('alias')} is not complete")
        if f.kind == "merged":
            inputs = {i.get("alias"): i for i in m["inputs"]}
            for kind, src in f.d["sources"].items():
                planned = self.planned_final(kind, f.dataset)
                if planned is None:
                    f.warn(f"no sibling {kind} plan to cross-check source")
                elif str(planned / "manifest.json") != src["manifest_path"]:
                    f.problem(
                        f"merged source {kind} is not the sibling plan's final: "
                        f"{src['manifest_path']}"
                    )
                if kind not in inputs or inputs[kind]["manifest_sha256"] != src["manifest_sha256"]:
                    f.problem(f"merged manifest input {kind} differs from the plan source")

    def planned_final(self, kind, dataset):
        for step, plan in self.plans.items():
            if plan is None or KIND_OF_STEP[step] != kind:
                continue
            for d in plan["datasets"]:
                if d["source"]["dataset_id"] == dataset:
                    return Path(d["final"])
        return None

    # ----------------------------------------------------------- index query
    def check_index_queries(self, f):
        ds = f.ds
        first = ds.scanner(
            columns=[c for c in ("target_id", "feature_key") if c in ds.schema.names], limit=1
        ).to_table()
        if first.num_rows != 1:
            f.problem("cannot read the first row for the index probe")
            return
        used = {}
        for i in ds.list_indices():
            col = i["fields"][0]
            if col not in first.column_names:
                continue
            value = first[col][0].as_py()
            scanner = ds.scanner(columns=[col], filter=f"{col} = '{value}'")
            plan = scanner.explain_plan(True)
            t0 = time.perf_counter()
            n = scanner.to_table().num_rows
            used[i["name"]] = dict(
                rows=n,
                index_used="ScalarIndexQuery" in plan,
                seconds=round(time.perf_counter() - t0, 3),
            )
            if n != 1:
                f.problem(f"index query on {col} returned {n} rows (expected 1)")
            if "ScalarIndexQuery" not in plan:
                f.warn(f"query plan on {col} does not use the scalar index")
        f.info["index_queries"] = used

    # ------------------------------------------------------------- full scan
    def scan_fragment(self, f, fr):
        batch_size = 16384 if f.kind == "text" else 4096
        h = hashlib.sha256()
        rows = nbytes = nulls = 0
        t0 = time.perf_counter()
        for batch in f.ds.scanner(
            fragments=[fr], batch_size=batch_size, scan_in_order=True, batch_readahead=2
        ).to_batches():
            rows += batch.num_rows
            nbytes += batch.nbytes
            col = batch.column("target_id")
            nulls += col.null_count
            h.update(ids_array(col).tobytes())
        result = dict(
            rows=rows,
            bytes=nbytes,
            digest=h.hexdigest(),
            nulls=nulls,
            seconds=time.perf_counter() - t0,
        )
        with self.lock:
            self.progress["fragments"] += 1
            self.progress["rows"] += rows
            self.progress["bytes"] += nbytes
        return result

    def run_scans(self, finals):
        jobs = []
        step = max(1, self.args.sample)
        for f in finals:
            for fr in f.fragments[::step]:
                jobs.append((f, fr))
        # Largest datasets first keeps the tail short.
        jobs.sort(key=lambda j: -j[0].manifest["rows"])
        self.progress["total_fragments"] = len(jobs)
        stop = threading.Event()

        def reporter():
            t0 = time.time()
            while not stop.wait(30):
                p = self.progress
                dt = time.time() - t0
                log(
                    f"scan {p['fragments']}/{p['total_fragments']} fragments, "
                    f"{p['rows'] / 1e6:.1f}M rows, {p['bytes'] / 1e9:.1f} GB arrow, "
                    f"{p['bytes'] / 1e9 / dt:.2f} GB/s, {p['rows'] / dt:.0f} rows/s"
                )

        threading.Thread(target=reporter, daemon=True).start()
        t0 = time.time()
        futures = [
            (f, fr.fragment_id, self.pool.submit(self.scan_fragment, f, fr)) for f, fr in jobs
        ]
        for f, fid, fut in futures:
            try:
                f.scan_results[fid] = fut.result()
            except Exception as e:  # noqa: BLE001
                f.problem(f"fragment {fid} scan failed: {type(e).__name__}: {str(e)[:300]}")
        stop.set()
        for f in finals:
            rs = f.scan_results
            f.scan = dict(
                fragments=len(rs),
                rows=sum(r["rows"] for r in rs.values()),
                bytes=sum(r["bytes"] for r in rs.values()),
                seconds=round(sum(r["seconds"] for r in rs.values()), 1),
            )
            nulls = sum(r["nulls"] for r in rs.values())
            if nulls:
                f.problem(f"{nulls} null target_id values")
            if step == 1 and f.scan["rows"] != f.manifest["rows"]:
                f.problem(
                    f"full scan read {f.scan['rows']} rows, manifest says {f.manifest['rows']}"
                )
            elif step > 1:
                f.warn(f"sampled scan: 1 in {step} fragments")
        return time.time() - t0

    def check_task_order(self, f):
        """Fragment digests vs the plan's pinned task order (1 task : 1 fragment, in order)."""
        if f.kind == "merged":
            return
        tasks = f.d["tasks"]
        frags = [f.scan_results.get(fr.fragment_id) for fr in f.fragments]
        sampled = self.args.sample > 1
        aligned = len(frags) == len(tasks) and all(
            r is None or r["rows"] == t["rows"] for r, t in zip(frags, tasks)
        )
        if aligned:
            pairs = [(r, t) for r, t in zip(frags, tasks) if r is not None]
            bad = sum(1 for r, t in pairs if r["digest"] != t["ordered_ids_sha256"])
            f.info["task_order"] = dict(
                mode="fragment_digest", tasks=len(tasks), checked=len(pairs), mismatched=bad
            )
            if bad == 0 or sampled:
                if bad:
                    f.problem(f"{bad} sampled fragments differ from the plan's task order")
                return
        elif sampled:
            f.warn("fragments are not 1:1 with tasks; order check needs a full run")
            return
        # Fallback: sequential id-only scan with the plan's set hash.
        log(
            f"{f.label}: fragments are not 1:1 with tasks or order differs; "
            "sorted-set hash fallback"
        )
        parts = [
            ids_array(b.column("target_id"))
            for b in f.ds.to_batches(columns=["target_id"], batch_size=65536)
        ]
        ids = np.concatenate(parts) if parts else np.empty(0, dtype="S64")
        try:
            set_hash = sorted_set_hash(ids)
        except ValueError as e:
            f.problem(str(e))
            return
        f.info["task_order"] = dict(mode="sorted_set_hash", tasks=len(tasks), fragments=len(frags))
        if set_hash != f.d["target_set_sha256"]:
            f.problem("target set differs from the plan (sorted-set hash)")
        else:
            f.warn("target order differs from the plan tasks; set matches")

    # ---------------------------------------------------------------- merged
    def check_merged(self, f):
        m, d = f.manifest, f.d
        subset = m["selection"]["mode"] == "subset"
        excluded = []
        for kind in ("codec", "speaker"):
            src = d["sources"][kind]
            sm = json.loads(Path(src["manifest_path"]).read_text())
            failed = sm["rows"] - sm["coverage"]["ok"]
            by_source = m["excluded_targets"]["by_source"].get(kind, {})
            if sum(by_source.values()) != failed:
                f.problem(
                    f"excluded_targets.by_source[{kind}] sums to {sum(by_source.values())}, "
                    f"source has {failed} failed"
                )
            if failed:
                table = lance.dataset(src["table_path"], version=src["lance_version"])
                ids = table.to_table(
                    columns=["target_id"], filter="status IS NULL OR status != 'ok'"
                )["target_id"]
                if len(ids) != failed:
                    f.problem(f"{kind} source has {len(ids)} non-ok rows, manifest says {failed}")
                excluded.append(ids_array(ids))
        excluded = np.unique(np.concatenate(excluded)) if excluded else np.empty(0, dtype="S64")
        if len(excluded) != d["excluded_count"]:
            f.problem(f"{len(excluded)} distinct excluded targets, plan says {d['excluded_count']}")
        text_src = d["sources"]["text"]
        text = lance.dataset(text_src["table_path"], version=text_src["lance_version"])
        targets = None
        if subset:
            t = m["selection"]["table"]
            tpath = f.final / "targets.lance"
            expected = f"features/merged/{d['run_id']}/targets.lance"
            if t["table_path"] != expected:
                f.problem(f"targets table_path {t['table_path']} != {expected}")
            if not tpath.exists():
                f.problem("targets.lance missing")
            else:
                targets = lance.dataset(str(tpath), version=t["lance_version"])
                if targets.count_rows() != m["rows"]:
                    f.problem(f"targets.lance rows {targets.count_rows()} != {m['rows']}")
                if self.schema_sha(targets.schema) != t["schema_sha256"]:
                    f.problem("targets.lance schema_sha256 differs")
                tmp = [e for e in os.listdir(tpath / "data") if e.startswith(".tmp")]
                if tmp:
                    f.problem(f"{len(tmp)} temp files in targets.lance/data")
                idx = {i["fields"][0] for i in targets.list_indices()}
                if "target_id" not in idx:
                    f.problem("targets.lance has no target_id index")

        def stream(ds, columns=("target_id",)):
            for b in ds.to_batches(columns=list(columns), batch_size=65536):
                yield b

        def text_filtered():
            for b in stream(text):
                ids = ids_array(b.column("target_id"))
                if len(excluded):
                    ids = ids[~member(ids, excluded)]
                yield ids

        def merged_ids():
            for b in stream(f.ds):
                yield ids_array(b.column("target_id"))

        def target_ids():
            if targets is None:
                return
            for b in stream(targets, ("target_kind", "target_id", "parent_sample_id")):
                kinds = b.column("target_kind").to_pylist()
                if any(k != "sample" for k in kinds):
                    f.problem("targets.lance has a non-sample target_kind")
                if not b.column("target_id").equals(b.column("parent_sample_id")):
                    f.problem("targets.lance parent_sample_id != target_id")
                yield ids_array(b.column("target_id"))

        class Stream:
            def __init__(self, gen):
                self.it, self.buf, self.done = iter(gen), np.empty(0, dtype="S64"), False

            def take(self, n):
                while len(self.buf) < n and not self.done:
                    try:
                        nxt = next(self.it)
                    except StopIteration:
                        self.done = True
                        break
                    self.buf = np.concatenate([self.buf, nxt]) if len(self.buf) else nxt
                out, self.buf = self.buf[:n], self.buf[n:]
                return out

        a, b, c = Stream(merged_ids()), Stream(text_filtered()), Stream(target_ids())
        total, mism_text, mism_targets, collected = 0, 0, 0, []
        while True:
            x = a.take(65536)
            if len(x) == 0:
                break
            y, z = b.take(len(x)), c.take(len(x))
            mism_text += int(len(y) != len(x) or np.any(x != y))
            if targets is not None:
                mism_targets += int(len(z) != len(x) or np.any(x != z))
            total += len(x)
            if total // 10_000_000 != (total - len(x)) // 10_000_000:
                log(f"merged {f.label}: {total / 1e6:.0f}M of {m['rows'] / 1e6:.1f}M rows compared")
            if subset:
                collected.append(x)
        if len(b.take(1)) or (targets is not None and len(c.take(1))):
            f.problem("text-minus-excluded or targets stream is longer than the merged table")
        if mism_text:
            f.problem(
                f"merged target_id stream differs from text minus excluded ({mism_text} blocks)"
            )
        if mism_targets:
            f.problem(f"merged target_id stream differs from targets.lance ({mism_targets} blocks)")
        f.info["merged_stream_rows"] = total
        if subset and collected:
            ids = np.concatenate(collected)
            try:
                h = sorted_set_hash(ids)
            except ValueError as e:
                f.problem(str(e))
                return
            if h != m["selection"]["target_set_sha256"]:
                f.problem("subset target_set_sha256 differs from the merged target_id set")

    # ----------------------------------------------------------- cross-check
    def cross_check(self):
        by = {}
        for f in self.finals:
            if f.manifest is not None:
                by.setdefault(f.dataset, {})[f.kind] = f
        table = {}
        for dataset in sorted(set(self.selected_rows) | set(by)):
            sel = self.selected_rows.get(dataset)
            row = dict(selected_rows=sel)
            fs = by.get(dataset, {})
            for kind in ("codec", "text", "speaker"):
                if kind in fs:
                    n = fs[kind].manifest["rows"]
                    row[kind] = n
                    row[f"{kind}_failed"] = fs[kind].manifest["coverage"]["failed"]
                    if sel is not None and n != sel:
                        fs[kind].problem(f"rows {n} != selection selected_rows {sel}")
            if "merged" in fs:
                mm = fs["merged"].manifest
                row["merged"] = mm["rows"]
                row["merged_excluded"] = mm["excluded_targets"]["total"]
                if sel is not None and mm["rows"] + mm["excluded_targets"]["total"] != sel:
                    fs["merged"].problem(f"merged rows + excluded != selected_rows {sel}")
            table[dataset] = row
        return table

    def check_work_root(self):
        if (self.work / "hold").exists():
            self.global_problems.append("WORK/hold exists")
        planned = {d["stage"] for p in self.plans.values() if p for d in p["datasets"]}
        root = Path(self.pipeline["root"])
        other = sorted(str(p) for p in root.glob("datasets/*/*/features/*/*.incomplete"))
        stray = [s for s in other if s not in planned]
        in_use = [s for s in other if s in planned]
        if in_use:
            self.global_warnings.append(
                f"{len(in_use)} planned stages still exist (unpublished datasets)"
            )
        if stray:
            self.global_warnings.append(
                f"{len(stray)} .incomplete stage dirs under the root not in this run's plans: "
                f"{stray[:5]}"
            )
        q = self.work / "quarantine"
        if q.exists():
            n = sum(len(files) for _, _, files in os.walk(q))
            if n:
                self.global_warnings.append(f"quarantine holds {n} files (cleanup item)")
        missing_plans = [s for s, p in self.plans.items() if p is None]
        if missing_plans:
            self.global_warnings.append(f"steps without a plan yet: {missing_plans}")

    # ------------------------------------------------------------------ main
    def run(self):
        t_start = time.time()
        self.load_plans()
        log(f"{len(self.finals)} plan datasets to audit, {self.args.threads} threads")
        published = []
        for f in self.finals:
            try:
                if self.check_metadata(f):
                    self.check_files(f)
                    self.check_inputs(f)
                    self.check_index_queries(f)
                    published.append(f)
            except Exception as e:  # noqa: BLE001
                f.problem(f"metadata phase crashed: {type(e).__name__}: {e}")
                traceback.print_exc()
            state = (
                "unpublished" if f.manifest is None else ("ok" if not f.problems else "PROBLEMS")
            )
            log(
                f"metadata {f.label}: {state} "
                f"({len(f.problems)} problems, {len(f.warnings)} warnings)"
            )
        t_meta = time.time() - t_start
        scan_seconds = 0.0
        if not self.args.no_scan and published:
            scan_seconds = self.run_scans(published)
            for f in published:
                try:
                    self.check_task_order(f)
                    if f.kind == "merged":
                        self.check_merged(f)
                except Exception as e:  # noqa: BLE001
                    f.problem(f"content phase crashed: {type(e).__name__}: {e}")
                    traceback.print_exc()
        counts = self.cross_check()
        self.check_work_root()
        runtime = time.time() - t_start
        problems = sum(len(f.problems) for f in self.finals) + len(self.global_problems)
        warnings = sum(len(f.warnings) for f in self.finals) + len(self.global_warnings)
        report = dict(
            generated_at=datetime.now(timezone.utc).isoformat(),
            work=str(self.work),
            args=vars(self.args),
            runtime_seconds=round(runtime, 1),
            metadata_seconds=round(t_meta, 1),
            scan_seconds=round(scan_seconds, 1),
            rows_scanned=self.progress["rows"],
            arrow_bytes_scanned=self.progress["bytes"],
            finals=[f.report() for f in self.finals],
            datasets=counts,
            global_problems=self.global_problems,
            global_warnings=self.global_warnings,
            summary=dict(
                clean=problems == 0,
                published=len(published),
                unpublished=len(self.finals) - len(published),
                problems=problems,
                warnings=warnings,
            ),
        )
        out = Path(self.args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"report: {out}")
        print(
            f"{'CLEAN' if problems == 0 else 'PROBLEMS'}: {len(published)} published finals "
            f"audited, {len(self.finals) - len(published)} unpublished, {problems} problems, "
            f"{warnings} warnings, {self.progress['rows'] / 1e6:.1f}M rows / "
            f"{self.progress['bytes'] / 1e9:.1f} GB read in {runtime:.0f}s"
        )
        for f in self.finals:
            for p in f.problems:
                print(f"  PROBLEM {f.label}: {p}")
            for w in f.warnings:
                print(f"  warning {f.label}: {w}")
        for p in self.global_problems:
            print(f"  PROBLEM work root: {p}")
        for w in self.global_warnings:
            print(f"  warning work root: {w}")
        return 0 if problems == 0 else 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--work", required=True)
    ap.add_argument(
        "--repo", default=str(DEFAULT_REPO), help="pipeline src dir (contract validators)"
    )
    ap.add_argument(
        "--kinds", default=None, help="comma list of codec,text,speaker,merged (default all)"
    )
    ap.add_argument("--datasets", default=None, help="comma list of dataset ids (default all)")
    ap.add_argument("--threads", type=int, default=48)
    ap.add_argument("--no-scan", action="store_true", help="metadata/file checks only")
    ap.add_argument(
        "--sample",
        type=int,
        default=1,
        help="scan 1 in N fragments (spot check; final audit uses 1)",
    )
    ap.add_argument("--require-all", action="store_true", help="an unpublished final is a problem")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "audit_report.json"))
    args = ap.parse_args()
    pa.set_cpu_count(max(4, min(32, os.cpu_count() or 4)))
    sys.exit(Audit(args).run())


if __name__ == "__main__":
    main()
