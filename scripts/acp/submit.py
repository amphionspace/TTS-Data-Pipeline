"""Render or submit one ACP job that processes a planned codec run on one 8-GPU node.

Run from the repository root: `python -m scripts.acp.submit --work <codec work> [--submit]`.
Default only renders the request. Submitting freezes src/scripts/configs into the work
directory so later repository edits cannot change a running job.
"""

import argparse
import datetime
import hashlib
import json
import shlex
import shutil
from pathlib import Path

import yaml

from .api import jobs_url, request

PROJECT = Path(__file__).resolve().parents[2]


def snapshot(destination):
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("src", "scripts", "configs"):
        shutil.copytree(
            PROJECT / name,
            destination / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    files = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }
    (destination / "source_manifest.json").write_text(json.dumps(files, indent=2) + "\n")
    return files


def payload(platform, name, runtime, work, workers_per_gpu, decode_threads, mode):
    command = [
        "bash",
        str(runtime / "scripts/acp/codec.sh"),
        str(runtime),
        str(work),
        str(workers_per_gpu),
        str(decode_threads),
        mode,
    ]
    return {
        "display_name": name,
        "framework": "PYTORCH",
        "roles": [
            {
                "name": "Worker",
                "resource_spec": [{"name": platform["worker_spec"]}],
                "total_replicas": 1,
                "image_path": platform["image"],
                "startup_script": shlex.join(command),
            }
        ],
        "env": [
            {"key": "PYTHON_BIN", "value": platform["python"]},
            {"key": "NUMBA_CACHE_DIR", "value": str(work / "numba-cache")},
        ],
        "mount": [
            {
                "type": "PV_AFS",
                "id": platform["volume_id"],
                "mount_path": platform["mount_path"],
                "subdir": "",
                "zone": platform["zone"],
            }
        ],
        "resource_pool": {"name": platform["cluster"], "zone": platform["zone"]},
        "scheduling": {"priority": "NORMAL", "quota_type": "RESERVED"},
        "ssh": {"auto_key_setup": platform["ssh_auto_key_setup"]},
        "fault_tolerance": {"backoff_limit": 0},
        "root_mapping": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="Planned codec work directory")
    parser.add_argument("--name", required=True, help="ACP display name")
    parser.add_argument("--config", type=Path, default=PROJECT / "configs/acp.yaml")
    parser.add_argument("--workers-per-gpu", type=int, choices=[1, 2, 4], default=2)
    parser.add_argument(
        "--decode-threads", default="auto", help="Per-worker decode threads, or auto from nproc"
    )
    parser.add_argument(
        "--mode",
        choices=["all", "encode"],
        default="all",
        help="all: copy reusable tasks and encode the rest on the node; encode: GPU tasks only",
    )
    parser.add_argument("--submit", action="store_true", help="Create the job; else only render")
    args = parser.parse_args()
    platform = yaml.safe_load(args.config.read_text())
    work = args.work.resolve()
    if not (work / "plan.json").exists():
        parser.error("work has no plan.json; plan the codec run first")
    if not work.is_relative_to(platform["mount_path"]):
        parser.error("work must be on the mounted ACP volume")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    submission = work / "submissions" / stamp
    runtime = submission / "runtime"
    body = payload(
        platform, args.name, runtime, work, args.workers_per_gpu, args.decode_threads, args.mode
    )
    print(json.dumps({"submit": args.submit, "url": jobs_url(platform), "body": body}, indent=2))
    if not args.submit:
        return
    files = snapshot(runtime)
    (submission / "request.json").write_text(json.dumps(body, indent=2) + "\n")
    # Never retry POST: a lost response can still mean a job was allocated.
    result = request("POST", jobs_url(platform), body, home=platform["home"])
    record = {"submitted_at": stamp, "files": len(files), "response": result}
    (submission / "job.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
