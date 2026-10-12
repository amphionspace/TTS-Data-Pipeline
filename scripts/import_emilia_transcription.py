"""Prepare and resume Emilia upstream transcription import, without inference."""

import argparse
import os
from pathlib import Path

for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(variable, "1")
# Lance 12 BTree creation stalls with both runtime pools restricted to one thread.
os.environ.setdefault("LANCE_CPU_THREADS", "2")
os.environ.setdefault("LANCE_IO_THREADS", "4")

import _bootstrap  # noqa: E402, F401

from tts_data_pipeline.annotations.qwen_asr.metadata_import import prepare, run  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--metadata-root", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--pilot", action="store_true", help="Allow an explicitly sampled fixture base")
    p = commands.add_parser("run")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--checkpoints-only", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.root, args.metadata_root, args.audit, args.work, pilot=args.pilot)
    else:
        run(args.work, args.workers, publish=not args.checkpoints_only)


if __name__ == "__main__":
    main()
