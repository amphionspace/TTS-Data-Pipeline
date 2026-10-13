"""Last resort: publish one codec or text dataset of a feature run from this host.

    devhost_publish.py --work W --step codec --dataset emilia

When a node keeps failing a publish on lost renames, the same pinned finalize can run on a host
whose renames are reliable, with the run's frozen runtime (validate_plan checks the code hash).
Two finalizes of one stage destroy each other's files, so this refuses unless the run's ACP job
is gone or terminal (FAILED, SUCCEEDED, STOPPED, DELETED; a suspended or stopping job can still run)
and holds an exclusive lock in the work root. The shared dev host can be slow when other users
load it; check its load first. Afterwards the final is verified and a verdict printed.
"""

import argparse
import fcntl
import importlib
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
# Only a gone or terminal job is safe; any other state (incl. unknown or stopping) may still run.
DONE = {None, "FAILED", "SUCCEEDED", "STOPPED", "DELETED"}
STEP_MODULES = {
    "codec": "tts_data_pipeline.codec.qwen3_12hz.run",
    "text": "tts_data_pipeline.text.run",
}


def job_state(work):
    """State of the run's latest ACP job, or None once the platform no longer knows it."""
    sys.path.insert(0, str(PROJECT))
    import yaml

    from scripts.acp.api import jobs_url, request

    platform = yaml.safe_load((PROJECT / "configs/acp.yaml").read_text())
    jobs = sorted((Path(work) / "submissions").glob("*/job.json"))
    if not jobs:
        return None
    name = json.loads(jobs[-1].read_text())["response"]["name"]
    try:
        return request("GET", f"{jobs_url(platform)}/{name}", home=platform["home"])["state"]
    except RuntimeError as error:
        if "404" in str(error):
            return None
        raise


def verify(run, p, d):
    """Problems with the published final; empty when it is intact."""
    import lance

    manifest = run.published_manifest(p, d)
    path = Path(d["final"]) / "features.lance"
    ds = lance.dataset(path, version=manifest["lance_version"])
    disk = set(os.listdir(path / "data"))
    missing = [f.path for fr in ds.get_fragments() for f in fr.metadata.files if f.path not in disk]
    problems = [f"missing data file {m}" for m in missing[:5]]
    if ds.tags.list().get(d["run_id"], {}).get("version") != manifest["lance_version"]:
        problems.append("run_id tag does not point to the manifest version")
    if Path(d["stage"]).exists():
        problems.append("the .incomplete stage still exists")
    return problems


def publish(work, step, dataset, *, state=job_state, gpus=8, workers_per_gpu=2):
    work = Path(work).resolve()
    current = state(work)
    if current not in DONE:
        raise SystemExit(f"The run's ACP job is {current}; delete it (and confirm 404) first")
    runtime = sorted((work / "submissions").glob("*/runtime"))
    src = runtime[-1] / "src" if runtime else PROJECT / "src"
    sys.path.insert(0, str(src))
    run = importlib.import_module(STEP_MODULES[step])
    with (work / "devhost-publish.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        steps = json.loads((work / "pipeline.json").read_text())["steps"]
        p = json.loads((Path(steps[step]) / "plan.json").read_text())
        run.validate_plan(p)
        d = next(x for x in p["datasets"] if x["source"]["dataset_id"] == dataset)
        started = time.perf_counter()
        print(f"publishing {step}/{dataset} with {run.__file__}", flush=True)
        if step == "codec":
            # As the node's run() records them, so the manifest describes the same launch.
            p["gpus"], p["workers_per_gpu"] = list(range(gpus)), workers_per_gpu
            run.finalize(p, d)
        else:
            if not run.process_dataset(p, d):
                raise SystemExit("Text dataset paused before publication")
        problems = verify(run, p, d)
        seconds = time.perf_counter() - started
        print(f"{step}/{dataset}: {'VERIFIED' if not problems else problems} in {seconds:.0f}s")
        return problems


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--work", required=True)
    parser.add_argument("--step", choices=sorted(STEP_MODULES), required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--gpus", type=int, default=8, help="the node launch, for the manifest")
    parser.add_argument("--workers-per-gpu", type=int, default=2)
    args = parser.parse_args()
    problems = publish(
        args.work, args.step, args.dataset, gpus=args.gpus, workers_per_gpu=args.workers_per_gpu
    )
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
