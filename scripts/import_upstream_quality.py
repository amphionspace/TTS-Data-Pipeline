"""Import the Emilia family's upstream DNSMOS into upstream_quality annotation tables.

    import_upstream_quality.py prepare --work W [--datasets emilia emilia_yodas emilia2]
    import_upstream_quality.py run --work W [--workers 24] [--no-publish] [--allow-non-ok]

prepare pins the base inputs and plan; run converts fragments into verified checkpoints
(resumable), refuses to publish when any row is not ok unless --allow-non-ok, then publishes
one results.lance per dataset and the global manifest. Spec: data contract 05.
"""

import argparse

import _bootstrap  # noqa: F401

from tts_data_pipeline.annotations.upstream_quality import run as upstream


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", default="/workspace/data/DATA-TTS-UNIFIED")
    p.add_argument("--work", required=True)
    p.add_argument("--datasets", nargs="+", default=list(upstream.SOURCES))
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("--workers", type=int, default=24)
    r.add_argument("--no-publish", action="store_true")
    r.add_argument("--allow-non-ok", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        plan = upstream.prepare(args.root, args.work, tuple(args.datasets))
        print(plan["run_id"])
    else:
        print(
            upstream.run(
                args.work,
                args.workers,
                publish=not args.no_publish,
                allow_non_ok=args.allow_non_ok,
            )
        )


if __name__ == "__main__":
    main()
