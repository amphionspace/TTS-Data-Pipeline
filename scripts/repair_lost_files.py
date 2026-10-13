"""Restore files a feature run lost to unreliable renames on the shared filesystem.

Run on a host whose renames are reliable (the dev host), beside a running or finished job:

    repair_lost_files.py --work W            # one dry-run scan
    repair_lost_files.py --work W --apply --every 120 --quarantine W/quarantine

See tts_data_pipeline.recovery for what is restored and the gates. Logs to W/repair.log; alerts
are logged once per distinct message. Never deletes; a quarantine is emptied by hand after the
run's audit passes.
"""

import argparse
import time
import traceback
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401

from tts_data_pipeline.recovery import Recovery


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-age", type=float, default=120)
    parser.add_argument("--every", type=float, default=0, help="seconds between scans; 0 = once")
    parser.add_argument("--quarantine", type=Path, help="hard-link aged .tmp files here")
    args = parser.parse_args()
    seen = set()

    def log(kind, message):
        if kind == "ALERT" and message in seen:
            return
        seen.add(message)
        line = f"{datetime.now().astimezone():%F %T %Z} {kind} {message}"
        print(line, flush=True)
        with (args.work / "repair.log").open("a") as stream:
            stream.write(line + "\n")

    while True:
        try:
            recovery = Recovery(
                args.work, apply=args.apply, min_age=args.min_age, quarantine=args.quarantine
            )
            for kind, message in recovery.scan():
                log(kind, message)
        except Exception:
            log("ALERT", f"scan failed, retrying next cycle: {traceback.format_exc()}")
        if not args.every:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
