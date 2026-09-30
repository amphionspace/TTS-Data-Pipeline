"""Materialize selected text/language as an independent resumable Lance feature table."""

import os

import _bootstrap  # noqa: F401

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
for name in ("RAYON_NUM_THREADS", "LANCE_CPU_THREADS", "LANCE_IO_THREADS", "TOKIO_WORKER_THREADS"):
    os.environ[name] = "2"

# ruff: noqa: E402
import argparse
from pathlib import Path

from tts_data_pipeline.text.run import event, prepare, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--targets-plan", type=Path, required=True)
    plan.add_argument("--work", type=Path, required=True)
    plan.add_argument("--output-root", type=Path)
    plan.add_argument("--datasets", nargs="+")
    execute = sub.add_parser("run")
    execute.add_argument("--work", type=Path, required=True)
    execute.add_argument("--workers", type=int, default=2)
    execute.add_argument("--max-tasks-per-dataset", type=int)
    args = parser.parse_args()
    if args.command == "plan":
        prepare(args.targets_plan, args.work, output_root=args.output_root, datasets=args.datasets)
    else:
        if args.workers < 1 or (
            args.max_tasks_per_dataset is not None and args.max_tasks_per_dataset < 1
        ):
            parser.error("workers and max-tasks-per-dataset must be positive")
        try:
            run(args.work, args.workers, args.max_tasks_per_dataset)
        except BaseException as exc:
            event(args.work, "failed", error_type=type(exc).__name__, error=str(exc))
            raise


if __name__ == "__main__":
    main()
