import importlib.util
import json
from pathlib import Path

import pytest
from test_text_run import plan

from tts_data_pipeline.feature_runtime import write_json

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/devhost_publish.py"
spec = importlib.util.spec_from_file_location("devhost_publish", SCRIPT)
devhost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devhost)


def work_root(tmp_path):
    _, p = plan(tmp_path)
    write_json(tmp_path / "pipeline.json", dict(steps=dict(text=str(p["work"]))))
    return p["datasets"][0]


@pytest.mark.parametrize("state", ["QUEUEING", "STARTING", "RUNNING", "SUSPENDED"])
def test_refuses_while_the_run_has_a_live_job(tmp_path, state):
    work_root(tmp_path)
    with pytest.raises(SystemExit, match=state):
        devhost.publish(tmp_path, "text", "example", state=lambda work: state)


def test_publishes_and_verifies_once_the_job_is_gone(tmp_path):
    d = work_root(tmp_path)
    assert devhost.publish(tmp_path, "text", "example", state=lambda work: None) == []
    manifest = json.loads((Path(d["final"]) / "manifest.json").read_text())
    assert manifest["rows"] == d["target_count"] and not Path(d["stage"]).exists()
