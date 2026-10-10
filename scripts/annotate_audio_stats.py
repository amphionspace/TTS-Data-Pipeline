"""Prepare or resume CPU audio statistics over published samples."""

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from tts_data_pipeline.annotations.audio_stats.run import prepare, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--output-root", type=Path)
    p.add_argument(
        "--sample-rows", type=int, default=0, help="Pilot rows per sampled fragment; 0 = all"
    )
    p = sub.add_parser("run")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.command == "prepare":
        plan = prepare(args.root, args.work, args.output_root, args.sample_rows)
        print(
            plan["run_id"],
            len(plan["inputs"]),
            "datasets",
            sum(s["target_rows"] for s in plan["inputs"]),
            "targets",
            flush=True,
        )
    else:
        run(args.work, args.workers)


if __name__ == "__main__":
    main()
