"""Build one selection's complete features in one job: codec, text, reference speaker, merged.

`init` (local) pins inputs and reusable published results into <work-root>/pipeline.json.
`run` (on the GPU node) schedules every step by its data dependencies:

    codec ─┬─ speaker (early datasets) ──┐
           │                             ├─ merged (early datasets)
    text ──┼─────────────────────────────┤
           └─ speaker (late datasets) ───┴─ merged (late datasets)

Each step is a resumable subprocess with its own work directory; rerunning `run` continues.
Early datasets are all but the ones with the most new encoding; their codec publishes first,
so their speaker and merge steps overlap the long encode instead of waiting for it.
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
    config = dict(
        created_at=datetime.now(timezone.utc).isoformat(),
        root=str(Path(args.root).resolve()),
        selection=str(Path(args.selection).resolve()),
        selection_sha256=file_hash(args.selection),
        models=str(Path(args.models).resolve()),
        codec_acceptance=str(Path(args.codec_acceptance).resolve()),
        speaker_acceptance=str(Path(args.speaker_acceptance).resolve()),
        reference_seed=args.reference_seed,
        codec_reuse=published(args.root, "codec", codec_ids - {None}),
        speaker_reuse=published(args.root, "speaker_embedding", {speaker_acceptance["profile_id"]}),
        late_datasets=args.late_datasets,
        # Smoke runs: a dataset subset written outside the unified root.
        datasets=args.datasets,
        output_root=str(Path(args.output_root).resolve()) if args.output_root else None,
        steps={
            k: str(work / k)
            for k in (
                "codec",
                "text",
                "speaker-early",
                "speaker-late",
                "merged-early",
                "merged-late",
            )
        },
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

    retries = 3

    def __init__(self, name, work, plan, run, log):
        self.name, self.work, self.plan_command, self.run_command = name, Path(work), plan, run
        self.log, self.process, self.started, self.failures = Path(log), None, False, 0
        self.failed, self.restart_at = False, None

    def start(self, python):
        self.python = python
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        with self.log.open("a") as handle:
            # A plan fixes run IDs; never re-plan an existing work directory.
            if not (self.work / "plan.json").exists():
                subprocess.run(
                    [python, *self.plan_command],
                    cwd=PROJECT,
                    check=True,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            # Own process group: a restart can clean up every child (workers holding GPUs).
            self.process = subprocess.Popen(
                [python, *self.run_command],
                cwd=PROJECT,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        self.started = True

    def finished(self):
        """Poll; restart a crashed step after cleanup and a pause, at most `retries` times."""
        if self.process is None or self.failed:
            return False
        if self.restart_at is not None:
            if time.monotonic() >= self.restart_at:
                self.restart_at = None
                self.start(self.python)
            return False
        code = self.process.poll()
        if code not in (None, 0):
            # Every step resumes from verified checkpoints, so a crashed worker (OOM, CUDA
            # error) costs only its in-flight tasks.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.failures += 1
            with self.log.open("a") as handle:
                handle.write(f"\n# exit code {code}; failure {self.failures}/{self.retries}\n")
            if self.failures > self.retries:
                self.failed = True
            else:
                self.restart_at = time.monotonic() + 60
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
            "all",
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
    out = ["--output-root", c["output_root"]] if c.get("output_root") else []
    codec.plan_command += out + (["--datasets", *c["datasets"]] if c.get("datasets") else [])
    codec.start(python)
    # Text needs only the pinned targets, which the codec plan fixes.
    text = Step(
        "text",
        s["text"],
        ["scripts/extract_text.py", "plan", "--targets-plan", str(codec_plan), "--work", s["text"]],
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
    all_datasets = {
        d["source"]["dataset_id"] for d in json.loads(codec_plan.read_text())["datasets"]
    }
    late = set(c["late_datasets"]) & all_datasets
    if not late:
        # Default: the dataset with the most new encoding finishes codec last.
        work_rows = {
            d["source"]["dataset_id"]: sum(t["rows"] - t.get("reuse_rows", 0) for t in d["tasks"])
            for d in json.loads(codec_plan.read_text())["datasets"]
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

    # While codec still encodes, speaker shares the GPUs with one worker per GPU.
    # Speaker-early overlaps codec on the same GPUs/CPUs: few GPU rows, so stay small.
    speaker_early = speaker("speaker-early", early, 1, 3) if early else None
    speaker_late = (
        speaker("speaker-late", late, args.speaker_workers_per_gpu, args.speaker_decode_threads)
        if late
        else None
    )
    merged_early = merged("merged-early", speaker_early, early) if early else None
    merged_late = merged("merged-late", speaker_late, late) if late else None
    steps = [x for x in (codec, text, speaker_early, speaker_late, merged_early, merged_late) if x]
    for step in steps[2:]:
        step.plan_command += out
    while True:
        done = {x.name for x in steps if x.finished()}
        codec_done = published_datasets(codec_plan)
        if speaker_early and not speaker_early.started and set(early) <= codec_done:
            speaker_early.start(python)
        if speaker_late and not speaker_late.started and "codec" in done:
            speaker_late.start(python)
        for m, sp in ((merged_early, speaker_early), (merged_late, speaker_late)):
            if m and not m.started and sp.name in done and "text" in done:
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
    a.add_argument("--datasets", nargs="+", help="Smoke runs only: process just these datasets")
    a.add_argument("--output-root", help="Smoke runs only: publish outside the unified root")
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
    args = parser.parse_args()
    init(args) if args.command == "init" else run(args)


if __name__ == "__main__":
    main()
