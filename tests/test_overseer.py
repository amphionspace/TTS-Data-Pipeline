import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/acp/overseer.py"
spec = importlib.util.spec_from_file_location("overseer", SCRIPT)
overseer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overseer)


def make(tmp_path, **kwargs):
    (tmp_path / "logs").mkdir(exist_ok=True)
    args = dict(work=str(tmp_path), interval=1, stall_minutes=30, memory_gb=800, watch=[])
    return overseer.Overseer(SimpleNamespace(**{**args, **kwargs}))


def wakes(tmp_path):
    path = tmp_path / "overseer.log"
    return [x for x in path.read_text().splitlines() if " WAKE " in x] if path.exists() else []


def test_wakes_on_new_failure_lines_only(tmp_path):
    (tmp_path / "logs").mkdir()
    log = tmp_path / "logs/codec.log"
    log.write_text("# exit code 1; failure 1/8\n")  # History before the overseer started.
    o = make(tmp_path)
    o.check_logs()
    assert wakes(tmp_path) == []
    with log.open("a") as stream:
        stream.write('{"phase": "all", "rows_done": 5}\n{"phase": "publish_failed", "da')
    o.check_logs()
    assert wakes(tmp_path) == []  # The failure line is not complete yet.
    with log.open("a") as stream:
        stream.write('taset": "x"}\n')
    o.check_logs()
    assert len(wakes(tmp_path)) == 1 and "publish_failed" in wakes(tmp_path)[0]


def test_failure_wakes_are_deduplicated_per_log_and_minute(tmp_path):
    (tmp_path / "logs").mkdir()
    log = tmp_path / "logs/text.log"
    o = make(tmp_path)
    log.write_text("Traceback (most recent call last):\n" * 5)  # A log created after start.
    o.check_logs()
    assert len(wakes(tmp_path)) == 1


def test_stall_wakes_only_for_an_unfinished_step(tmp_path):
    (tmp_path / "codec").mkdir()
    (tmp_path / "text").mkdir()
    for step in ("codec", "text"):
        (tmp_path / step / "status.json").write_text(json.dumps(dict(rows_done=10)))
    (tmp_path / "status.json").write_text(json.dumps(dict(finished=["text"])))
    o = make(tmp_path, stall_minutes=0)
    o.check_stalls()
    for step in o.progress:
        o.progress[step] = (o.progress[step][0], time.time() - 60)
    o.check_stalls()
    assert len(wakes(tmp_path)) == 1 and "codec rows_done 10" in wakes(tmp_path)[0]


def test_dead_watched_process_wakes(tmp_path):
    o = make(tmp_path, watch=["no-such-process-pattern-1f3a"])
    o.check_processes()
    assert "watched process not running" in wakes(tmp_path)[0]
