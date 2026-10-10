"""Append failure evidence immediately; checkpoints remain the authoritative outcomes."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from ...timestamps import BEIJING


class FailureJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.seen = set()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                item = json.loads(line)
                self.seen.add((item["target_id"], item["error_code"], item["stage"]))

    def record(self, target, code, stage="primary", details=None, from_checkpoint=False):
        key = (target["target_id"], code, stage)
        with self.lock:
            if from_checkpoint and key in self.seen:
                return
            item = dict(
                at=datetime.now(BEIJING).isoformat(),
                target_id=target["target_id"],
                input_fingerprint=target["input_fingerprint"],
                error_code=code,
                stage=stage,
                origin="checkpoint" if from_checkpoint else "request",
                details=details,
            )
            with self.path.open("a") as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.seen.add(key)


def record(plan, target, code, stage="primary", details=None):
    journal = plan.get("_failure_journal")
    if journal is not None:
        journal.record(target, code, stage, details)
    print(
        json.dumps(
            dict(
                event="asr_item_failed", target_id=target["target_id"], stage=stage, error_code=code
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )


def failed(plan, target, code, details=None):
    record(plan, target, code, details=details)
    return dict(target, status="failed", error_code=code, item_count=0), None
