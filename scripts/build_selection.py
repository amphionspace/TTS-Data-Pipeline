"""Build a pinned supervised selection in explicit, restartable phases."""

import argparse
import os
import traceback
from pathlib import Path

# Bound nested native concurrency before importing Arrow/Lance or starting workers.
for variable in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "LANCE_CPU_THREADS",
):
    os.environ[variable] = "1"

os.environ["LANCE_IO_THREADS"] = "2"
os.environ["TOKIO_WORKER_THREADS"] = "2"

import _bootstrap  # noqa: E402, F401

from tts_data_pipeline.selection import event, plan, publish, resolve_duplicates, scan  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["plan", "scan", "deduplicate", "publish", "run"])
    parser.add_argument("--root", default="/workspace/data/DATA-TTS-UNIFIED")
    parser.add_argument("--work", required=True)
    parser.add_argument("--rules", default="configs/selections/first-v0.1.json")
    parser.add_argument("--workers", type=int, default=256)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    try:
        if args.phase == "plan":
            plan(args.root, args.work, args.rules)
        if args.phase in {"scan", "run"}:
            scan(args.work, args.workers)
        if args.phase in {"deduplicate", "run"}:
            resolve_duplicates(args.work)
        if args.phase in {"publish", "run"}:
            publish(args.work, min(args.workers, 16))
    except Exception as exc:
        if Path(args.work).is_dir():
            event(args.work, "failed", operation=args.phase, error=str(exc))
        traceback.print_exc()
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
