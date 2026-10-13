"""Watch a feature job: quiet while healthy, a WAKE line when a person should look.

    setsid nohup python scripts/acp/overseer.py --work W --watch 'repair_lost_files.py' &
    tail -F W/overseer.log | grep --line-buffered ' WAKE '

Read-only; it never stops, submits or deletes anything. Every --interval seconds it writes
WAKE lines (deduplicated) to W/overseer.log for:
- an ACP job state other than QUEUEING, STARTING or RUNNING (SUCCEEDED included);
- a log line in W/logs/*.log matching a failure pattern, tailed from the end of the file;
- a --watch process pattern with no running process;
- a step whose rows_done has not moved for --stall-minutes while the job runs and is not held;
- container memory from the node monitor above --memory-gb.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
LIVE = {"QUEUEING", "STARTING", "RUNNING"}
FAILURE = re.compile(r'Traceback|"phase": "failed"|publish_failed|# exit code|ALERT')


class Tail:
    """New complete lines of a growing file, from `offset` (its end when the overseer started)."""

    def __init__(self, path, offset=0):
        self.path, self.offset = path, offset

    def lines(self):
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(size - self.offset)
        end = data.rfind(b"\n") + 1
        self.offset += end
        return data[:end].replace(b"\0", b"").decode(errors="replace").splitlines()


class Overseer:
    def __init__(self, args):
        self.args, self.work = args, Path(args.work).resolve()
        self.woken, self.state, self.progress = {}, None, {}
        # Logs that exist at start are history; a log that appears later is read from its start.
        self.tails = {p: Tail(p, p.stat().st_size) for p in (self.work / "logs").glob("*.log")}

    def log(self, kind, message, key=None, every=None):
        if kind == "WAKE":
            key = key or message
            last = self.woken.get(key)
            if last is not None and (every is None or time.time() - last < every):
                return
            self.woken[key] = time.time()
        line = f"{datetime.now().astimezone():%F %T %Z} {kind} {message}"[:600]
        with (self.work / "overseer.log").open("a") as stream:
            stream.write(line + "\n")

    def job_state(self):
        out = subprocess.run(
            [sys.executable, "-m", "scripts.acp.status", "--work", str(self.work)],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        return json.loads(out.stdout)["state"]

    def check_logs(self):
        for path in sorted((self.work / "logs").glob("*.log")):
            tail = self.tails.setdefault(path, Tail(path))
            for line in tail.lines():
                if FAILURE.search(line):
                    self.log("WAKE", f"[{path.stem}] {line}", key=f"log {path.stem}", every=60)

    def check_processes(self):
        for pattern in self.args.watch:
            if subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode:
                self.log("WAKE", f"watched process not running: {pattern}", every=3600)

    def check_stalls(self):
        try:
            finished = set(json.loads((self.work / "status.json").read_text())["finished"])
        except (OSError, ValueError, KeyError):
            finished = set()
        now = time.time()
        for status in sorted(self.work.glob("*/status.json")):
            step = status.parent.name
            try:
                rows = json.loads(status.read_text()).get("rows_done")
            except (OSError, ValueError):
                continue
            value, since = self.progress.get(step, (None, now))
            if rows != value or step in finished:
                self.progress[step] = (rows, now)
            elif now - since > self.args.stall_minutes * 60:
                minutes = (now - since) / 60
                self.log(
                    "WAKE",
                    f"{step} rows_done {rows} unchanged for {minutes:.0f} min",
                    key=f"stall {step}",
                    every=3600,
                )

    def check_memory(self):
        logs = sorted((self.work / "acp-monitor").glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return
        values = re.findall(r"cgroup_mem_bytes (\d+)", logs[-1].read_text(errors="replace"))
        if values and int(values[-1]) > self.args.memory_gb * 1e9:
            gb = int(values[-1]) / 1e9
            self.log("WAKE", f"container memory {gb:.0f} GB", key="memory", every=1800)

    def cycle(self):
        state = self.job_state()
        if state != self.state:
            self.log("INFO", f"job state {self.state} -> {state}")
            if state not in LIVE:
                self.log("WAKE", f"job state {state}", key=f"state {state}")
            self.state = state
        self.check_logs()
        self.check_processes()
        if state == "RUNNING" and not (self.work / "hold").exists():
            self.check_stalls()
        else:
            self.progress = {}
        self.check_memory()

    def run(self):
        self.log("INFO", f"overseer started for {self.work}")
        while True:
            try:
                self.cycle()
            except Exception:
                self.log("WAKE", f"overseer error: {traceback.format_exc()[-400:]}", every=900)
            time.sleep(self.args.interval)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--work", required=True)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--stall-minutes", type=float, default=30)
    parser.add_argument("--memory-gb", type=float, default=800)
    parser.add_argument("--watch", nargs="*", default=[], help="pgrep -f patterns to keep alive")
    Overseer(parser.parse_args()).run()


if __name__ == "__main__":
    main()
