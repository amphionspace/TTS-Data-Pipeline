"""Publish a separate merged feature table, retaining codec/speaker/text sources."""

import os

import _bootstrap  # noqa: F401

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "1"
for name in ("RAYON_NUM_THREADS", "LANCE_CPU_THREADS", "LANCE_IO_THREADS", "TOKIO_WORKER_THREADS"):
    os.environ[name] = "2"

# Large string BTREE builds need more than the default per-partition sort pool.
os.environ.setdefault("LANCE_MEM_POOL_SIZE", str(16 * 1024**3))

# ruff: noqa: E402
import argparse

from tts_data_pipeline.merge_features import prepare, run

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    for name in ("codec-plan", "speaker-plan", "text-plan", "work"):
        plan.add_argument("--" + name, required=True)
    plan.add_argument("--output-root")
    plan.add_argument("--datasets", nargs="+")
    plan.add_argument("--batch-rows", type=int, default=32768)
    execute = commands.add_parser("run")
    execute.add_argument("--work", required=True)
    execute.add_argument("--workers", type=int, default=2)
    args = vars(parser.parse_args())
    command = args.pop("command")
    (prepare if command == "plan" else run)(**args)
