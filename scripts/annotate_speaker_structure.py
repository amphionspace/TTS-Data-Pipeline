"""MOSS speaker_structure (diarization) of emilia, emilia_yodas and emilia2 via a MOSS service.

    annotate_speaker_structure.py prepare --work W [--base-url http://127.0.0.1:18000]
    annotate_speaker_structure.py run --work W        # a new round over every sample not ok yet
    annotate_speaker_structure.py publish --work W    # publishes the last round's results

Datasets run one at a time with the profile's concurrency. A request is resent on timeouts,
unusable output or suspected truncation; service outages pause all requests (checked every
30 minutes, 8 times) and never mark samples failed. Spec: data contract 05.
"""

import argparse

import _bootstrap  # noqa: F401

from tts_data_pipeline.annotations.speaker_structure import run as diar


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", default="/workspace/data/DATA-TTS-UNIFIED")
    p.add_argument("--work", required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:18000")
    p.add_argument("--server-config", default="configs/moss-transcribe-diarize-v1.server.json")
    p.add_argument("--datasets", nargs="+", default=list(diar.DATASETS))
    for name in ("run", "publish"):
        sub.add_parser(name).add_argument("--work", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        plan = diar.prepare(
            args.root, args.work, args.base_url, args.server_config, tuple(args.datasets)
        )
        print(plan["run_id"])
    elif args.command == "run":
        print(diar.run(args.work))
    else:
        print(diar.publish(args.work))


if __name__ == "__main__":
    main()
