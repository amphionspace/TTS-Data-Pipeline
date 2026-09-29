"""Production selection boundary cases and complete two-dataset publication."""

import json
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.schema import digest
from tts_data_pipeline.selection import (
    classify,
    file_sha,
    plan,
    publish,
    resolve_duplicates,
    scan,
    write_json,
)


def rules():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "configs/selections/first-v0.1.json").read_text())


def row(name, audio, text="hello", language="en", duration=2.0):
    return {
        "sample_id": digest(name),
        "audio_sha256": digest(audio),
        "text": text,
        "language": language,
        "num_frames": int(duration * 16000),
        "sample_rate": 16000,
        "duration_seconds": duration,
        "parent_sample_id": None,
        "segment_start_frame": None,
        "segment_end_frame": None,
    }


@pytest.mark.parametrize("duration,expected", [(0.999, 3001), (1, 0), (120, 0), (120.001, 3002)])
def test_inclusive_duration_policy(duration, expected):
    value = row("sample", "audio", duration=duration)
    assert classify(value, "alpha", rules(), set(), {})[0] == expected


def test_primary_reason_preserves_all_flags_and_exact_exclusion():
    value = row("sample", "audio", text=" \t\n", duration=0.5)
    reason, flags, language, text = classify(value, "alpha", rules(), {value["audio_sha256"]}, {})
    assert reason == 103
    assert flags == (1 | (1 << 9) | (1 << 1) | (1 << 4))
    assert text == ""
    exclusion = {
        "scope": {
            "text_revision": digest(
                ["selected-text-v1", "different", rules()["text_normalization"]]
            )
        }
    }
    with pytest.raises(ValueError, match="revision mismatch"):
        classify(value, "alpha", rules(), set(), {("alpha", value["sample_id"]): exclusion})


def fixture_root(tmp_path, rows):
    root = tmp_path / "unified"
    for name, records in rows.items():
        release = root / "datasets" / name / "v0.1"
        release.mkdir(parents=True)
        ds = lance.write_dataset(
            pa.Table.from_pylist(records),
            release / "samples.lance",
            data_storage_version="2.2",
            max_rows_per_file=2,
        )
        ds.create_scalar_index("sample_id", "BTREE")
        write_json(
            release / "manifest.json",
            {
                "status": "complete",
                "contract_version": "v0.1",
                "table_path": "samples.lance",
                "lance_version": ds.version,
                "rows": len(records),
            },
        )
    configuration = rules()
    configuration["source_priority"] = list(rows)
    rp = tmp_path / "rules.json"
    write_json(rp, configuration)
    return root, rp


def test_global_duplicates_text_overrides_and_restartable_publication(tmp_path):
    alpha = [
        row("a0", "same", text=" \thello\n", language="en-US"),
        row("a1", "conflict", text="yes"),
        row("a2", "minimum", duration=1),
        row("a3", "maximum", duration=120),
        row("a4", "blank", text=" \t "),
        row("a5", "too-short", duration=0.5),
    ]
    beta = [
        row("b0", "same"),
        row("b1", "conflict", text="no"),
        row("b2", "too-short", duration=0.5),
        row("b3", "long", duration=121),
    ]
    root, rp = fixture_root(tmp_path, {"alpha": alpha, "beta": beta})
    work = tmp_path / "scratch"
    p = plan(root, work, rp, selection_id="test-selection")
    originals = {s["manifest_path"]: file_sha(root / s["manifest_path"]) for s in p["inputs"]}
    scan(work, workers=2)
    scan(work, workers=2)  # verified scratch checkpoints, no duplicate outputs
    result = resolve_duplicates(work)
    assert result["groups"] == 3 and result["members"] == 6 and result["conflicting_groups"] == 1
    assert resolve_duplicates(work)["members"] == 6
    manifest = publish(work, workers=2)
    assert manifest["status"] == "complete"
    assert sum(o["selected_rows"] for o in manifest["outputs"]) == 3
    assert manifest["text_sources"]["0"]["kind"] == "base_normalization"
    outcomes = {}
    for output in manifest["outputs"]:
        ds = lance.dataset(root / output["table_path"], version=output["base_version"])
        branch = ds.checkout_version((output["branch"], output["lance_version"]))
        outcomes.update({r["sample_id"]: r for r in branch.to_table().to_pylist()})
        assert "selection_reason" not in ds.schema.names
        assert output["rows"] == ds.count_rows()
    assert outcomes[digest("a0")]["selection_reason"] == 0
    assert outcomes[digest("a0")]["selected_text"] == "hello"
    assert outcomes[digest("a0")]["selected_text_source"] == 0
    assert outcomes[digest("b0")]["selection_reason"] == 4001
    assert (
        outcomes[digest("a1")]["selection_reason"]
        == outcomes[digest("b1")]["selection_reason"]
        == 4002
    )
    assert outcomes[digest("a4")]["selection_reason"] == 1001
    assert outcomes[digest("a4")]["selected_text"] == ""
    assert outcomes[digest("a5")]["selection_flags"] & (1 << 5)
    assert all(file_sha(root / path) == sha for path, sha in originals.items())
    with pytest.raises(ValueError, match="already published"):
        publish(work, workers=2)


def test_distinct_full_hashes_with_identical_prefix_do_not_deduplicate(tmp_path):
    a = row("a", "one")
    b = row("b", "two")
    a["audio_sha256"] = "a" * 16 + "1" * 48
    b["audio_sha256"] = "a" * 16 + "2" * 48
    root, rp = fixture_root(tmp_path, {"alpha": [a, b]})
    work = tmp_path / "scratch"
    plan(root, work, rp, selection_id="test-no-duplicates")
    scan(work, workers=1)
    assert resolve_duplicates(work)["members"] == 0
    manifest = publish(work, workers=1)
    assert manifest["outputs"][0]["selected_rows"] == 2
