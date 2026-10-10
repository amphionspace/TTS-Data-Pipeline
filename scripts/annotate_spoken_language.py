"""Prepare or resume FireRedLID annotation of the four published game datasets."""

import argparse

import _bootstrap  # noqa: F401

from tts_data_pipeline.annotations.firered_lid.run import prepare, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--root", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--upstream-code", required=True)
    p.add_argument("--output-root")
    p.add_argument("--sample-rows", type=int, default=0)
    p = commands.add_parser("run")
    p.add_argument("--work", required=True)
    p.add_argument(
        "--gpus", required=True, help="Comma-separated GPU indices; repeats allow replicas"
    )
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--frame-budget", type=int, default=32000)
    p.add_argument("--cpu-threads", type=int, default=8)
    args = parser.parse_args()
    if args.command == "prepare":
        plan = prepare(
            args.root,
            args.work,
            args.model_dir,
            args.upstream_code,
            args.output_root,
            args.sample_rows,
        )
        print(plan["run_id"], sum(s["target_rows"] for s in plan["inputs"]), flush=True)
    else:
        run(
            args.work,
            [int(g) for g in args.gpus.split(",")],
            args.batch_size,
            args.frame_budget,
            args.cpu_threads,
        )


if __name__ == "__main__":
    main()
