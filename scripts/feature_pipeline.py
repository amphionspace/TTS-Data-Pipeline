"""Build one selection's complete features in one job: codec, text, reference speaker, merged.

`init` (local) pins inputs and reusable published results into <work-root>/pipeline.json.
`run` (on the GPU node) schedules every step by its data dependencies:

    codec ──(all published)──┬─ speaker (early datasets) ──┬─ merged (early datasets)
    text ────────────────────┼─────────────────────────────┤
                             └─ speaker (late datasets) ───┴─ merged (late datasets)

Each step is a resumable subprocess with its own work directory; rerunning `run` continues.
Speaker for the early datasets starts once their codec is published, beside the late encoding;
speaker for the late datasets starts after the whole codec run.
Early datasets are all but the ones with the most new encoding: mostly reuse copies, so
their speaker and merge finish while the late datasets are still extracting speaker.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

from tts_data_pipeline.feature_runtime import file_hash, write_json

PROJECT = Path(__file__).resolve().parents[1]
CODEC_PROFILE = "qwen3-12hz-24k-k16-fp32-packed-actual-v2"
SPEAKER_PROFILE = "qwen3-ecapa-24k-fp32-reference-v1"


def published(root, kind, profile_ids):
    """Latest complete published manifest per dataset of `kind` with an allowed profile."""
    chosen = {}
    for path in sorted(Path(root).glob(f"datasets/*/*/features/{kind}/*/manifest.json")):
        m = json.loads(path.read_text())
        if m.get("status") == "complete" and m["profile_id"] in profile_ids:
            finished = datetime.fromisoformat(m["finished_at"])
            old = chosen.get(m["dataset_id"])
            if old is None or finished > old[1]:
                chosen[m["dataset_id"]] = (str(path), finished)
    return sorted(path for path, _ in chosen.values())


def init(args):
    work = Path(args.work_root).resolve()
    if (work / "pipeline.json").exists():
        raise FileExistsError(work / "pipeline.json")
    codec_acceptance = json.loads(Path(args.codec_acceptance).read_text())
    speaker_acceptance = json.loads(Path(args.speaker_acceptance).read_text())
    codec_ids = {codec_acceptance["profile_id"], codec_acceptance.get("predecessor_profile_id")}
    steps = {
        k: str(work / k)
        for k in ("codec", "text", "speaker-early", "speaker-late", "merged-early", "merged-late")
    }
    pinned = None
    if args.pinned_work_root:
        # Speaker and merged only, on another work root's published codec and text.
        old = json.loads((Path(args.pinned_work_root) / "pipeline.json").read_text())
        if not args.datasets:
            raise ValueError("A pinned run needs --datasets")
        if old["selection_sha256"] != file_hash(args.selection) or args.output_root:
            raise ValueError("A pinned run keeps the pinned run's selection and output root")
        pinned = dict(work_root=str(Path(args.pinned_work_root).resolve()))
        for kind in ("codec", "text"):
            steps[kind] = old["steps"][kind]
            plan = Path(steps[kind]) / "plan.json"
            missing = set(args.datasets) - published_datasets(plan)
            if missing:
                raise ValueError(f"Pinned {kind} is not published for {sorted(missing)}")
            pinned[f"{kind}_plan_sha256"] = file_hash(plan)
    config = dict(
        created_at=datetime.now(timezone.utc).isoformat(),
        root=str(Path(args.root).resolve()),
        selection=str(Path(args.selection).resolve()),
        selection_sha256=file_hash(args.selection),
        models=str(Path(args.models).resolve()),
        codec_acceptance=str(Path(args.codec_acceptance).resolve()),
        speaker_acceptance=str(Path(args.speaker_acceptance).resolve()),
        reference_seed=args.reference_seed,
        reference_placement=args.reference_placement,
        pinned=pinned,
        codec_reuse=published(args.root, "codec", codec_ids - {None}),
        speaker_reuse=published(args.root, "speaker_embedding", {speaker_acceptance["profile_id"]}),
        late_datasets=args.late_datasets,
        # Smoke runs: a dataset subset written outside the unified root.
        datasets=args.datasets,
        output_root=str(Path(args.output_root).resolve()) if args.output_root else None,
        steps=steps,
    )
    work.mkdir(parents=True, exist_ok=True)
    write_json(work / "pipeline.json", config)
    print(
        json.dumps(
            {k: v for k, v in config.items() if k not in ("codec_reuse", "speaker_reuse")}, indent=2
        )
    )
    print(
        f"codec reuse sources: {len(config['codec_reuse'])}, "
        f"speaker reuse sources: {len(config['speaker_reuse'])}"
    )


class Step:
    """One resumable subprocess; started once, polled by the scheduler."""

    # On ACP nodes the quarkfs client fails or loses renames; one lost file fails a whole
    # all-or-nothing codec publish (~22 min for 10k files), so publishes need room to retry.
    retries = 8
    # A run this long made progress: a later crash starts a fresh retry budget, so sporadic
    # crashes over a 13 h codec run cannot exhaust it.
    healthy_seconds = 1800

    def __init__(self, name, work, plan, run, log):
        self.name, self.work, self.plan_command, self.run_command = name, Path(work), plan, run
        self.log, self.process, self.started, self.failures = Path(log), None, False, 0
        self.failed, self.restart_at = False, None
        # Optional callable: a start or restart waits while it returns a reason to wait.
        self.gate = None
        # Optional status.json with rows_done events: a run that advanced it, or published a
        # dataset, made progress, and its crash does not consume the retry budget.
        self.progress_status = None
        self.progressing_failures = 0

    def start(self, python):
        self.python, self.started = python, True
        self.started_at = time.monotonic()
        self.started_wall = datetime.now(timezone.utc)
        self.rows_base = self.rows_best = None
        self.log_offset = self.log.stat().st_size if self.log.exists() else 0
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        with self.log.open("a") as handle:
            # A plan fixes run IDs; never re-plan an existing work directory.
            if not (self.work / "plan.json").exists():
                code = subprocess.run(
                    [python, *self.plan_command],
                    cwd=PROJECT,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                ).returncode
                if code:
                    # A failed plan is this step's failure, not the scheduler's.
                    self.process = None
                    self.failure(handle, f"plan exit code {code}")
                    return
            # Own process group: a restart can clean up every child (workers holding GPUs).
            self.process = subprocess.Popen(
                [python, *self.run_command],
                cwd=PROJECT,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )

    def failure(self, handle, reason):
        self.failures += 1
        handle.write(f"\n# {reason}; failure {self.failures}/{self.retries}\n")
        handle.flush()
        if self.failures > self.retries:
            self.failed = True
        else:
            self.restart_at = time.monotonic() + 60

    def poll_progress(self):
        """Track rows_done of this start's own events (the first one is the baseline)."""
        try:
            event = json.loads(Path(self.progress_status).read_text())
            at = datetime.fromisoformat(event["at"])
            rows = event["rows_done"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        if at < self.started_wall:
            return
        if self.rows_base is None:
            self.rows_base = rows
        self.rows_best = max(self.rows_best or rows, rows)

    def progressed(self):
        published = 0
        if self.log.exists():
            with self.log.open("rb") as stream:
                stream.seek(self.log_offset)
                published = stream.read().count(b'"phase": "dataset_published"')
        rows = self.rows_base is not None and self.rows_best > self.rows_base
        return rows or published > 0

    def finished(self):
        """Poll; restart a crashed step after cleanup and a pause, at most `retries` times."""
        if self.failed:
            return False
        if self.progress_status and self.process is not None and self.restart_at is None:
            self.poll_progress()
        if self.restart_at is not None:
            if time.monotonic() >= self.restart_at:
                reason = self.gate and self.gate()
                if reason:
                    print(
                        f"{datetime.now(timezone.utc).isoformat()} {self.name} held: {reason}",
                        flush=True,
                    )
                    self.restart_at = time.monotonic() + 30
                    return False
                self.restart_at = None
                self.start(self.python)
            return False
        if self.process is None:
            return False
        code = self.process.poll()
        if code not in (None, 0):
            # Every step resumes from verified checkpoints, so a crashed worker (OOM, CUDA
            # error) costs only its in-flight tasks.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if time.monotonic() - self.started_at > self.healthy_seconds:
                self.failures = 0
            reason = f"exit code {code}"
            if self.progress_status and self.progressed():
                # Lost renames fail publishes while encoding still advances: never give up on
                # a step that keeps making progress, but say so loudly when it keeps crashing.
                self.failures = 0
                self.progressing_failures += 1
                reason += f" after progress (budget reset, {self.progressing_failures} so far)"
                if self.progressing_failures >= 20:
                    reason += "; ALERT: still crashing after progress 20+ times"
            with self.log.open("a") as handle:
                self.failure(handle, reason)
            return False
        return code == 0


def reuse_for(manifests, datasets):
    """Reuse manifests of just these datasets (path: datasets/<dataset_id>/<release>/...)."""
    return [m for m in manifests if Path(m).parts[Path(m).parts.index("datasets") + 1] in datasets]


def published_datasets(plan_path):
    """Datasets of a run plan whose final manifest exists."""
    plan = json.loads(Path(plan_path).read_text())
    return {
        d["source"]["dataset_id"]
        for d in plan["datasets"]
        if (Path(d["final"]) / "manifest.json").exists()
    }


def unpublished_complete(plan_path):
    """Datasets whose every task is checkpointed but that have no final yet: publish candidates."""
    plan = json.loads(Path(plan_path).read_text())
    waiting = []
    for d in plan["datasets"]:
        checkpoints = Path(d["state"]) / "checkpoints"
        if (Path(d["final"]) / "manifest.json").exists() or not checkpoints.exists():
            continue
        if len(list(checkpoints.glob("*.json"))) >= len(d["tasks"]):
            waiting.append(d["source"]["dataset_id"])
    return waiting


def run(args):
    work = Path(args.work_root).resolve()
    c = json.loads((work / "pipeline.json").read_text())
    if file_hash(c["selection"]) != c["selection_sha256"]:
        raise ValueError("Selection changed since init")
    python, gpus = sys.executable, [str(g) for g in range(args.gpus)]
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    s = c["steps"]
    codec_plan = Path(s["codec"]) / "plan.json"
    text_plan = Path(s["text"]) / "plan.json"

    def status(phase, **fields):
        write_json(
            work / "status.json",
            dict(phase=phase, at=datetime.now(timezone.utc).isoformat(), **fields),
        )

    pinned = c.get("pinned")
    out = ["--output-root", c["output_root"]] if c.get("output_root") else []
    if pinned:
        for kind, path in (("codec", codec_plan), ("text", text_plan)):
            if file_hash(path) != pinned[f"{kind}_plan_sha256"]:
                raise ValueError(f"Pinned {kind} plan changed")
            if not set(c["datasets"]) <= published_datasets(path):
                raise ValueError(f"Pinned {kind} is not published")
        codec = text = None
    else:
        codec = Step(
            "codec",
            s["codec"],
            [
                "scripts/extract_codec.py",
                "plan",
                "--root",
                c["root"],
                "--selection",
                c["selection"],
                "--work",
                s["codec"],
                "--models",
                c["models"],
                "--acceptance",
                c["codec_acceptance"],
                "--reuse",
                *c["codec_reuse"],
            ],
            [
                "scripts/extract_codec.py",
                "run",
                "--work",
                s["codec"],
                "--mode",
                args.codec_mode,
                "--gpus",
                *gpus,
                "--workers-per-gpu",
                str(args.codec_workers_per_gpu),
                "--decode-threads",
                str(args.codec_decode_threads),
                "--cpu-workers",
                str(args.copy_workers),
            ],
            logs / "codec.log",
        )
        codec.plan_command += out + (["--datasets", *c["datasets"]] if c.get("datasets") else [])
        codec.progress_status = Path(s["codec"]) / "status.json"
        if args.codec_mode == "encode" and codec_plan.exists():
            # The reuse mode runs on another host and publishes there. A codec start publishes every
            # fully checkpointed, unpublished dataset, so every (re)start here waits until none is
            # left: two hosts must never publish one stage.
            def gate():
                waiting = unpublished_complete(codec_plan)
                write_json(
                    work / "codec-gate.json",
                    dict(at=datetime.now(timezone.utc).isoformat(), waiting=waiting),
                )
                return waiting and f"datasets awaiting publication by the reuse host: {waiting}"

            codec.gate = gate
            codec.python, codec.started, codec.started_at = python, True, time.monotonic()
            codec.restart_at = time.monotonic()
        else:
            codec.start(python)
        if not codec_plan.exists():
            raise SystemExit(f"Codec plan failed; see {logs / 'codec.log'}")
        # Text needs only the pinned targets, which the codec plan fixes.
        text = Step(
            "text",
            s["text"],
            [
                "scripts/extract_text.py",
                "plan",
                "--targets-plan",
                str(codec_plan),
                "--work",
                s["text"],
            ],
            [
                "scripts/extract_text.py",
                "run",
                "--work",
                s["text"],
                "--workers",
                str(args.text_workers),
            ],
            logs / "text.log",
        )
        text.plan_command += out
        text.start(python)
    all_datasets = (
        set(c["datasets"])
        if pinned
        else {d["source"]["dataset_id"] for d in json.loads(codec_plan.read_text())["datasets"]}
    )
    late = set(c["late_datasets"]) & all_datasets
    if not late:
        # Default: the dataset with the most new encoding finishes codec last.
        work_rows = {
            d["source"]["dataset_id"]: sum(t["rows"] - t.get("reuse_rows", 0) for t in d["tasks"])
            for d in json.loads(codec_plan.read_text())["datasets"]
            if d["source"]["dataset_id"] in all_datasets
        }
        late = {max(work_rows, key=work_rows.get)}
    early = sorted(all_datasets - late)
    late = sorted(late)

    def speaker(name, datasets, workers_per_gpu, decode_threads):
        return Step(
            name,
            s[name],
            [
                "scripts/extract_speaker.py",
                "plan",
                "--targets-plan",
                str(codec_plan),
                "--work",
                s[name],
                "--models",
                c["models"],
                "--acceptance",
                c["speaker_acceptance"],
                "--reference-codec-plan",
                str(codec_plan),
                "--reference-seed",
                str(c["reference_seed"]),
                "--reference-placement",
                c.get("reference_placement", "uniform"),
                "--datasets",
                *datasets,
                *(
                    ["--reuse", *reuse]
                    if (reuse := reuse_for(c["speaker_reuse"], datasets))
                    else []
                ),
            ],
            [
                "scripts/extract_speaker.py",
                "run",
                "--work",
                s[name],
                "--mode",
                "all",
                "--gpus",
                *gpus,
                "--workers-per-gpu",
                str(workers_per_gpu),
                "--memory-fraction",
                str(args.speaker_memory_fraction),
                "--decode-threads",
                str(decode_threads),
                "--cpu-workers",
                str(args.copy_workers),
            ],
            logs / f"{name}.log",
        )

    def merged(name, speaker_step, datasets):
        return Step(
            name,
            s[name],
            [
                "scripts/merge_features.py",
                "plan",
                "--codec-plan",
                str(codec_plan),
                "--speaker-plan",
                str(Path(speaker_step.work) / "plan.json"),
                "--text-plan",
                str(text_plan),
                "--work",
                s[name],
                "--datasets",
                *datasets,
            ],
            [
                "scripts/merge_features.py",
                "run",
                "--work",
                s[name],
                "--workers",
                str(args.merge_workers),
            ],
            logs / f"{name}.log",
        )

    # Speaker-early runs beside codec encoding and speaker-late on the same GPUs: few GPU
    # rows, so stay small.
    speaker_early = speaker("speaker-early", early, 1, 3) if early else None
    speaker_late = (
        speaker("speaker-late", late, args.speaker_workers_per_gpu, args.speaker_decode_threads)
        if late
        else None
    )
    merged_early = merged("merged-early", speaker_early, early) if early else None
    merged_late = merged("merged-late", speaker_late, late) if late else None
    steps = [x for x in (codec, text, speaker_early, speaker_late, merged_early, merged_late) if x]
    for step in steps:
        if step not in (codec, text):
            step.plan_command += out
    while True:
        done = {x.name for x in steps if x.finished()}
        codec_done = published_datasets(codec_plan)
        # In encode mode the codec step can end before the reuse host has published.
        if (
            speaker_early
            and not speaker_early.started
            # Early datasets are mostly reuse copies: their speaker runs beside the late
            # datasets' encoding, in its own process group, as soon as their codec is published.
            and set(early) <= codec_done
        ):
            speaker_early.start(python)
        if (
            speaker_late
            and not speaker_late.started
            and (pinned or "codec" in done)
            and set(late) <= codec_done
        ):
            speaker_late.start(python)
        for m, sp in ((merged_early, speaker_early), (merged_late, speaker_late)):
            if m and not m.started and sp.name in done and (pinned or "text" in done):
                m.start(python)
        status(
            "running",
            finished=sorted(done),
            running=sorted(x.name for x in steps if x.started and x.name not in done),
            codec_published=sorted(codec_done),
            early=early,
            late=late,
        )
        failed = sorted(x.name for x in steps if x.failed)
        # A step that has not started is blocked when any of its inputs failed or is blocked.
        needs = {
            speaker_early: [codec],
            speaker_late: [codec],
            merged_early: [speaker_early, text],
            merged_late: [speaker_late, text],
        }
        blocked = set()
        for step in (speaker_early, speaker_late, merged_early, merged_late):
            if (
                step
                and not step.started
                and any(x and (x.failed or x.name in blocked) for x in needs[step])
            ):
                blocked.add(step.name)
        if len(done) + len(failed) + len(blocked) >= len(steps):
            break
        time.sleep(30)
    if failed:
        status("failed", finished=sorted(done), failed=failed, blocked=sorted(blocked))
        raise SystemExit(f"Steps failed after retries: {failed}; see {logs}")
    status("complete", finished=sorted(x.name for x in steps))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("init")
    a.add_argument("--work-root", required=True)
    a.add_argument("--root", default="/workspace/data/DATA-TTS-UNIFIED")
    a.add_argument("--selection", required=True)
    a.add_argument("--models", default=str(PROJECT / "cache/feature-models"))
    a.add_argument("--codec-acceptance", required=True)
    a.add_argument("--speaker-acceptance", required=True)
    a.add_argument("--reference-seed", type=int, default=20261008)
    a.add_argument(
        "--datasets", nargs="+", help="Smoke or pinned runs only: process just these datasets"
    )
    a.add_argument("--output-root", help="Smoke runs only: publish outside the unified root")
    a.add_argument(
        "--reference-placement",
        choices=["uniform", "prefix"],
        default="uniform",
        help="prefix: every reference starts at frame 0, with the uniform policy's lengths",
    )
    a.add_argument(
        "--pinned-work-root",
        help="Run only speaker and merged on this work root's published codec and text plans",
    )
    a.add_argument(
        "--late-datasets",
        nargs="*",
        default=[],
        help="Datasets whose speaker/merge wait for the whole codec run",
    )
    # Defaults fit an 80-vCPU node: codec decode needs ~16 cores at 10k audio-h/h,
    # copies are IO-bound; nproc inside the container reports the host, not the quota.
    r = sub.add_parser("run")
    r.add_argument("--work-root", required=True)
    r.add_argument("--gpus", type=int, default=8)
    r.add_argument("--codec-workers-per-gpu", type=int, default=2)
    r.add_argument("--codec-decode-threads", type=int, default=4)
    r.add_argument("--speaker-workers-per-gpu", type=int, default=2)
    r.add_argument("--speaker-decode-threads", type=int, default=6)
    r.add_argument("--speaker-memory-fraction", type=float, default=0.15)
    r.add_argument("--copy-workers", type=int, default=24)
    r.add_argument("--text-workers", type=int, default=8)
    r.add_argument("--merge-workers", type=int, default=16)
    r.add_argument(
        "--codec-mode",
        choices=["all", "encode"],
        default="all",
        help="encode: this node only encodes; another host runs `extract_codec.py run --mode "
        "reuse` and publishes, and codec (re)starts wait while any dataset awaits publication",
    )
    args = parser.parse_args()
    init(args) if args.command == "init" else run(args)


if __name__ == "__main__":
    main()
