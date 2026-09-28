import argparse
from pathlib import Path

from .adapters import ADAPTERS
from .inventory import audit, scalar_audit


def main():
    parser = argparse.ArgumentParser(description="TTS source audit and unified data conversion")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "scalars"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("preview", help="Validate at most 32 real records")
    command.add_argument("dataset", choices=sorted(ADAPTERS))
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--config")
    command.add_argument("--snapshot", required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--limit", type=int, default=4)
    command = commands.add_parser("convert", help="Convert and fully verify a small corpus/config")
    command.add_argument("dataset", choices=sorted(ADAPTERS))
    command.add_argument(
        "--root", type=Path, required=True, help="Dataset root, not split directory"
    )
    command.add_argument("--config", help="all (default) or local source subset, e.g. dev.clean")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--shard-mib", type=int, default=1024)
    command.add_argument("--deep-verify", action="store_true")
    command = commands.add_parser("bulk", help="Parallel full conversion into one Lance table")
    command.add_argument("dataset", choices=sorted(ADAPTERS))
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--workers", type=int, default=32)
    command.add_argument("--shard-mib", type=int, default=1024)
    command.add_argument("--batch-mib", type=int, default=4096)
    command.add_argument("--resume", action="store_true")
    command.add_argument("--exclusions", type=Path, help="Explicit source exclusion policy JSON")
    command.add_argument("--deep-verify", action="store_true")
    args = parser.parse_args()
    if args.command == "inventory":
        audit(args.root, args.output)
    elif args.command == "scalars":
        scalar_audit(args.root, args.output)
    elif args.command == "preview":
        import json

        from .preview import preview

        print(
            json.dumps(
                preview(
                    args.dataset, args.root, args.snapshot, args.output, args.limit, args.config
                ),
                indent=2,
            )
        )
    elif args.command == "convert":
        import json

        from .convert import convert

        result = convert(
            args.dataset,
            args.root,
            args.output,
            args.config,
            args.shard_mib * 1024 * 1024,
            deep_verify=args.deep_verify,
        )
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in ("status", "rows", "duration_seconds", "output_bytes", "validation")
                },
                indent=2,
            )
        )
    elif args.command == "bulk":
        from .bulk import bulk_convert

        bulk_convert(
            args.dataset,
            args.root,
            args.output,
            args.workers,
            args.shard_mib * 1024**2,
            args.batch_mib * 1024**2,
            args.resume,
            args.deep_verify,
            exclusions_path=args.exclusions,
        )
