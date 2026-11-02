"""Plan, resume and publish dots.tts AudioVAE posteriors and x-vectors for one base release."""

import os

import _bootstrap  # noqa: F401

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
for name in ("RAYON_NUM_THREADS", "LANCE_CPU_THREADS", "LANCE_IO_THREADS", "TOKIO_WORKER_THREADS"):
    os.environ[name] = "2"
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# ruff: noqa: E402
import argparse
from pathlib import Path

from tts_data_pipeline.dots_vae.run import prepare, run

ROOT = Path("/workspace/data/DATA-TTS-UNIFIED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--dataset", required=True)
    plan.add_argument("--work", type=Path, required=True)
    plan.add_argument("--model", type=Path, required=True, help="dots.tts-soar directory")
    plan.add_argument("--root", type=Path, default=ROOT)
    plan.add_argument("--output-root", type=Path, help="publish elsewhere, e.g. a smoke test")
    plan.add_argument("--task-size", type=int, default=1024)
    go = sub.add_parser("run")
    go.add_argument("--work", type=Path, required=True)
    go.add_argument("--gpus", type=int, nargs="+", required=True)
    go.add_argument("--workers-per-gpu", type=int, default=1)
    go.add_argument("--cpu-threads", type=int, default=8)
    go.add_argument("--max-tasks", type=int, help="stop after this many tasks, unpublished")
    args = parser.parse_args()
    if args.command == "plan":
        prepare(
            args.root,
            args.dataset,
            args.work,
            args.model,
            task_size=args.task_size,
            output_root=args.output_root,
        )
    else:
        run(
            args.work,
            args.gpus,
            workers_per_gpu=args.workers_per_gpu,
            cpu_threads=args.cpu_threads,
            max_tasks=args.max_tasks,
        )


if __name__ == "__main__":
    main()
