"""Plan, resume, and publish codec runs from a fixed selection."""

import os

import _bootstrap  # noqa: F401

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
for name in ("RAYON_NUM_THREADS", "LANCE_CPU_THREADS", "LANCE_IO_THREADS", "TOKIO_WORKER_THREADS"):
    os.environ[name] = "4"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/tts-codec-numba-cache")
# Without it the caching allocator fragments to ~70 GB reserved per GPU with 2 workers
# (artifacts/codec-oom-review); the accepted actual-v2 run used it. Allocation only, no numerics.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# ruff: noqa: E402
import argparse
from pathlib import Path

from tts_data_pipeline.codec.qwen3_12hz.run import event, prepare, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--root", type=Path, default=Path("/workspace/data/DATA-TTS-UNIFIED"))
    plan.add_argument("--selection", type=Path, required=True)
    plan.add_argument("--work", type=Path, required=True)
    plan.add_argument(
        "--models", type=Path, default=Path(__file__).resolve().parents[1] / "cache/feature-models"
    )
    plan.add_argument("--acceptance", type=Path, required=True)
    plan.add_argument("--output-root", type=Path)
    plan.add_argument("--datasets", nargs="+")
    plan.add_argument("--precision", choices=["bf16", "fp32"], default="fp32")
    plan.add_argument("--task-size", type=int, default=4096)
    plan.add_argument(
        "--reuse",
        type=Path,
        nargs="+",
        default=[],
        help="Published codec manifests whose results are copied for the same audio",
    )
    execute = sub.add_parser("run")
    execute.add_argument("--work", type=Path, required=True)
    execute.add_argument("--gpus", type=int, nargs="+", default=list(range(8)))
    execute.add_argument("--decode-threads", type=int, default=8)
    execute.add_argument("--workers-per-gpu", type=int, choices=[1, 2, 4], default=1)
    execute.add_argument(
        "--mode",
        choices=["all", "reuse", "encode"],
        default="all",
        help="reuse: copy-only tasks on CPU; encode: tasks needing GPUs",
    )
    execute.add_argument("--cpu-workers", type=int, default=32)
    args = parser.parse_args()
    if args.command == "plan":
        if args.task_size < 1:
            parser.error("task-size must be positive")
        prepare(
            args.root,
            args.selection,
            args.work,
            args.models,
            args.acceptance,
            precision=args.precision,
            task_size=args.task_size,
            output_root=args.output_root,
            datasets=args.datasets,
            reuse=args.reuse,
        )
    else:
        if not args.gpus or len(set(args.gpus)) != len(args.gpus) or min(args.gpus) < 0:
            parser.error("GPU IDs must be unique nonnegative integers")
        if args.decode_threads < 1:
            parser.error("decode-threads must be positive")
        try:
            run(
                args.work,
                args.gpus,
                args.decode_threads,
                args.workers_per_gpu,
                mode=args.mode,
                cpu_workers=args.cpu_workers,
            )
        except Exception as exc:
            event(args.work, "failed", error_type=type(exc).__name__, error=str(exc))
            raise


if __name__ == "__main__":
    main()
