"""selection-v4: speaker_structure input, exactly one speaker required (6002)."""

import json
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.schema import digest
from tts_data_pipeline.selection import plan, publish, resolve_duplicates, scan, write_json
from test_selection_filter import asr, row, rules, write_annotation, write_base, write_parent


def write_speakers(root, run_id, base, records, counts):
    """counts: speaker_count per record, or None for a failed diarization row."""
    path = Path("datasets") / base["dataset_id"] / "v0.1/annotations/speaker_structure" / run_id
    rows = [
        {
            "sample_id": r["sample_id"],
            "status": "ok" if c is not None else "failed",
            "error_code": None if c is not None else "model_invalid_output",
            "result": {"audio_sha256": r["audio_sha256"], "speaker_count": c}
            if c is not None
            else None,
        }
        for r, c in reversed(list(zip(records, counts, strict=True)))
    ]
    ds = lance.write_dataset(pa.Table.from_pylist(rows), root / path / "results.lance")
    folder = root / "annotations/speaker_structure" / run_id
    folder.mkdir(parents=True)
    schema = [
        {
            "name": "result",
            "fields": [{"name": "audio_sha256"}, {"name": "speaker_count"}],
        }
    ]
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "run_id": run_id,
            "profile_id": "moss",
            "inputs": [base],
            "outputs": [
                {
                    "dataset_id": base["dataset_id"],
                    "table": {
                        "table_path": str(path / "results.lance"),
                        "branch": None,
                        "lance_version": ds.version,
                        "rows": len(rows),
                        "schema": schema,
                    },
                }
            ],
        },
    )


def speaker_rules(**changes):
    configuration = rules()
    configuration.update(
        parent_selection="parent",
        source_priority=["alpha"],
        annotation_inputs=[
            {"text_source": 1, "task": "transcription", "run_id": "asr-run", "datasets": ["alpha"]}
        ],
        speaker_inputs=[
            {
                "task": "speaker_structure",
                "run_id": "moss-run",
                "datasets": ["alpha"],
                "speaker_count": 1,
            }
        ],
        rules_version="selection-v4",
    )
    configuration.pop("source_timeline_evidence")
    configuration["text_sources"] = {"1": configuration["text_sources"]["1"]}
    priority = configuration["reason_priority"]
    priority.insert(priority.index(3002) + 1, 6002)
    configuration.update(changes)
    return configuration


def test_only_single_speaker_rows_are_selected(tmp_path):
    root = tmp_path / "unified"
    records = [
        row("single", text="one voice"),
        row("dialog", text="two voices"),
        row("failed", text="no answer"),
        row("singing", text="la la la"),
        # Same audio: the would-be representative has two speakers, its copy only one.
        row("copy-a", audio="shared", text="shared words"),
        row("copy-b", audio="shared", text="shared words"),
    ]
    counts = [1, 2, None, 0, 2, 1]
    base = write_base(root, "alpha", records)
    write_annotation(root, "asr-run", base, records, [asr(r, text=r["text"]) for r in records])
    write_speakers(root, "moss-run", base, records, counts)
    # An inherited exclusion of a dataset outside this selection matches no row here.
    other = {"dataset_id": "beta", "release_id": "v0.1", "sample_id": digest("elsewhere")}
    write_parent(
        root,
        [{"exclusion_id": digest(["selection-exclusion-v1", other, 105]), "scope": other,
          "reason": 105}],
    )
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, speaker_rules())
    work = tmp_path / "scratch"
    p = plan(root, work, rules_path, selection_id="v4")
    assert [a["run_id"] for a in p["annotation_inputs"]] == ["asr-run"]
    assert [a["run_id"] for a in p["speaker_inputs"]] == ["moss-run"]
    scan(work, workers=2)
    resolve_duplicates(work)
    manifest = json.loads(json.dumps(publish(work, workers=2)))  # as written: string keys
    (output,) = manifest["outputs"]
    ds = lance.dataset(root / output["table_path"], version=output["base_version"])
    branch = ds.checkout_version((output["branch"], output["lance_version"]))
    reasons = {
        r["sample_id"]: r["selection_reason"]
        for r in branch.to_table(columns=["sample_id", "selection_reason"]).to_pylist()
    }
    assert reasons == {
        digest("single"): 0,
        digest("dialog"): 6002,
        digest("failed"): 6002,
        digest("singing"): 6002,
        digest("copy-a"): 6002,
        digest("copy-b"): 0,  # the speaker rule runs before duplicate resolution
    }
    # Consumers that read one text source per dataset are unaffected.
    assert [a["run_id"] for a in manifest["annotation_inputs"]] == ["asr-run"]
    assert manifest["speaker_inputs"][0]["run_id"] == "moss-run"
    assert manifest["reason_dictionary"]["version"] == 5
    assert manifest["reason_dictionary"]["codes"]["6002"] == "not_single_speaker"
    assert manifest["flag_dictionary"]["bits"]["15"] == "speaker_policy"
    assert manifest["coverage"]["speaker_structure"]["outcomes"] == {
        "passed": 2,
        "count_2": 2,
        "count_0": 1,
        "unavailable": 1,
    }


def test_speaker_rule_needs_every_dataset_covered(tmp_path):
    root = tmp_path / "unified"
    write_parent(root, [])
    for name in ("alpha", "beta"):
        write_base(root, name, [row(name + "0")])
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, speaker_rules(source_priority=["alpha", "beta"]))
    with pytest.raises(ValueError, match="cover every dataset"):
        plan(root, tmp_path / "scratch", rules_path, selection_id="x")


def test_speaker_rule_needs_its_reason_code(tmp_path):
    configuration = speaker_rules()
    configuration["reason_priority"].remove(6002)
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, configuration)
    with pytest.raises(ValueError, match="6002"):
        plan(tmp_path / "unified", tmp_path / "scratch", rules_path, selection_id="x")
