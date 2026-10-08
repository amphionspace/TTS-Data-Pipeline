"""Plan, resume, and publish FP32 speaker embeddings without batch padding."""

import os

import _bootstrap  # noqa: F401

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
for name in ("RAYON_NUM_THREADS", "LANCE_CPU_THREADS", "LANCE_IO_THREADS", "TOKIO_WORKER_THREADS"):
    os.environ[name] = "2"
os.environ.setdefault("LANCE_MEM_POOL_SIZE", str(16 * 1024**3))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/tts-speaker-numba")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# ruff: noqa: E402
import argparse
from pathlib import Path

from tts_data_pipeline.speaker.qwen3_ecapa.run import event, prepare, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--targets-plan", type=Path, required=True)
    plan.add_argument("--work", type=Path, required=True)
    plan.add_argument(
        "--models", type=Path, default=Path(__file__).resolve().parents[1] / "cache/feature-models"
    )
    plan.add_argument("--acceptance", type=Path, required=True)
    plan.add_argument("--output-root", type=Path)
    plan.add_argument("--datasets", nargs="+")
    plan.add_argument("--reference-sources-plan", type=Path)
    plan.add_argument("--reference-seed", type=int, default=20261008)
    execute = sub.add_parser("run")
    execute.add_argument("--work", type=Path, required=True)
    execute.add_argument("--gpus", type=int, nargs="+", default=list(range(1, 8)))
    execute.add_argument("--decode-threads", type=int, default=16)
    execute.add_argument("--max-tasks", type=int)
    execute.add_argument("--workers-per-gpu", type=int, default=1)
    execute.add_argument("--memory-fraction", type=float, default=0.25)
    execute.add_argument("--mel-frame-budget", type=int, default=90000)
    args = parser.parse_args()
    if args.command == "plan":
        prepare(
            args.targets_plan,
            args.work,
            args.models,
            args.acceptance,
            output_root=args.output_root,
            datasets=args.datasets,
            reference_sources_plan=args.reference_sources_plan,
            reference_seed=args.reference_seed,
        )
    else:
        if not args.gpus or len(set(args.gpus)) != len(args.gpus) or min(args.gpus) < 0:
            parser.error("GPU IDs must be unique nonnegative integers")
        if args.decode_threads < 1 or (args.max_tasks is not None and args.max_tasks < 1):
            parser.error("decode-threads and max-tasks must be positive")
        try:
            if (
                not 0 < args.memory_fraction <= 1
                or args.mel_frame_budget < 5
                or args.workers_per_gpu < 1
            ):
                parser.error("Invalid memory fraction or mel frame budget")
            run(
                args.work,
                args.gpus,
                args.decode_threads,
                max_tasks=args.max_tasks,
                memory_fraction=args.memory_fraction,
                mel_frame_budget=args.mel_frame_budget,
                workers_per_gpu=args.workers_per_gpu,
            )
        except BaseException as exc:
            event(args.work, "failed", error_type=type(exc).__name__, error=str(exc))
            raise


if __name__ == "__main__":
    main()
