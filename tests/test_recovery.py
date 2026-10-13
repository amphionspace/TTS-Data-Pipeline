import json
import os
import time
from pathlib import Path

from test_text_run import plan

from tts_data_pipeline.feature_runtime import write_json
from tts_data_pipeline.recovery import Recovery
from tts_data_pipeline.text import run


def work_root(tmp_path):
    """A text work root with one checkpointed task and a pipeline.json naming the step."""
    _, p = plan(tmp_path)
    d = p["datasets"][0]
    run.process_dataset(p, d, max_tasks=1)
    write_json(tmp_path / "pipeline.json", dict(steps=dict(text=str(p["work"]))))
    cp = next((Path(d["state"]) / "checkpoints").glob("*.json"))
    data = Path(d["stage"]) / "features.lance/data"
    return d, cp, data, json.loads(cp.read_text())["files"][0]


def lose(path, *, age=600):
    """What a lost rename leaves: the full source under a temporary name, aged."""
    temp = path.with_name(".tmpLOST01")
    os.rename(path, temp)
    old = time.time() - age
    os.utime(temp, (old, old))
    return temp


def kinds(events):
    return [k for k, _ in events]


def test_restores_lost_data_file_from_identical_temp(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    temp = lose(data / f["name"])
    assert kinds(Recovery(tmp_path).scan()) == ["DRY-RUN"]
    assert temp.exists() and not (data / f["name"]).exists()
    assert kinds(Recovery(tmp_path, apply=True).scan()) == ["RESTORED"]
    assert (data / f["name"]).exists() and not temp.exists()
    assert Recovery(tmp_path, apply=True).scan() == []


def test_never_restores_a_same_size_temp_with_other_content(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    temp = lose(data / f["name"])
    content = bytearray(temp.read_bytes())
    content[-1] ^= 1
    temp.write_bytes(bytes(content))
    os.utime(temp, (time.time() - 600,) * 2)
    assert kinds(Recovery(tmp_path, apply=True).scan()) == ["ALERT"]
    assert not (data / f["name"]).exists()


def test_leaves_a_young_temp_to_its_writer(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    lose(data / f["name"], age=0)
    assert Recovery(tmp_path, apply=True).scan() == []


def test_skips_tasks_the_pipeline_already_redoes(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    lose(data / f["name"])
    (Path(d["state"]) / "lost_files.jsonl").write_text(json.dumps(dict(task=cp.stem)) + "\n")
    assert Recovery(tmp_path, apply=True).scan() == []


def test_quarantined_copy_survives_cleanup_of_the_temp(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    temp = lose(data / f["name"])
    quarantine = tmp_path / "quarantine"
    Recovery(tmp_path, apply=True, quarantine=quarantine).quarantine_temps(d, data)
    temp.unlink()  # What codec publish cleanup does to every unreferenced .tmp.
    assert kinds(Recovery(tmp_path, apply=True, quarantine=quarantine).scan()) == ["RESTORED"]
    assert (data / f["name"]).exists()


def test_restores_lost_checkpoint_json_only_when_verify_would_accept_it(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    temp = cp.with_name(f"{cp.name}.1.2.tmp")
    os.rename(cp, temp)
    os.utime(temp, (time.time() - 600,) * 2)
    assert kinds(Recovery(tmp_path, apply=True).scan()) == ["RESTORED"]
    assert cp.exists()

    bad = json.loads(cp.read_text())
    bad["profile_id"] = "0" * 64
    cp.unlink()
    temp.write_text(json.dumps(bad))
    os.utime(temp, (time.time() - 600,) * 2)
    assert kinds(Recovery(tmp_path, apply=True).scan()) == ["ALERT"]
    assert not cp.exists()


def test_alerts_on_missing_file_without_any_source(tmp_path):
    d, cp, data, f = work_root(tmp_path)
    (data / f["name"]).unlink()
    events = Recovery(tmp_path, apply=True).scan()
    assert kinds(events) == ["ALERT"] and "without a matching source" in events[0][1]
