"""Prepare or resume independent Qwen ASR annotation runs."""

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from tts_data_pipeline.annotations.qwen_asr.migrate import migrate_tables
from tts_data_pipeline.annotations.qwen_asr.run import prepare, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--work", type=Path, required=True)
    command.add_argument("--model-root", type=Path, required=True)
    command.add_argument("--endpoint", default="http://127.0.0.1:18101/v1")
    command.add_argument(
        "--datasets",
        nargs="+",
        default=["genshin_voice", "starrail_voice", "wutheringwaves", "zenless_voice"],
    )
    command.add_argument("--batch-size", type=int, default=512)
    command = commands.add_parser("run")
    command.add_argument("--work", type=Path, required=True)
    command.add_argument("--workers", type=int, default=128)
    command.add_argument(
        "--auto-resume",
        action="store_true",
        help="Resume verified checkpoints after temporary service failures",
    )
    command = commands.add_parser(
        "migrate-tables", help="Publish a v1 run as a single v2 table, without ASR"
    )
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--source-manifest", type=Path, required=True)
    command.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        plan = prepare(
            args.root, args.work, args.model_root, args.endpoint, args.datasets, args.batch_size
        )
        print(plan["run_id"], sum(s["rows"] for s in plan["inputs"]), "targets", flush=True)
    elif args.command == "migrate-tables":
        print(migrate_tables(args.root, args.source_manifest, args.work), flush=True)
    else:
        run(args.work, args.workers, args.auto_resume)


if __name__ == "__main__":
    main()
