import json
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.annotations.flexivoice_text import convert


@pytest.mark.parametrize(
    ("text", "status", "canonical"),
    [
        ("[inaudible] hello [LAUGHTER] [BREATHING]", "ok", "hello [Laughter] [Breathing]"),
        ("hello", "ok", None),
        ("[inaudible]", "skipped", None),
        ("hello [GARBAGE]", "skipped", None),
        ("hello [garbage]", "skipped", None),
        ("[UNRECOGNIZED] hello", "skipped", None),
    ],
)
def test_text_revision_records_repairs_and_unusable_transcripts(text, status, canonical):
    row = {"sample_id": "a", "audio_sha256": "b", "language": None, "text": text}
    output = convert(row)
    assert output["status"] == status
    assert (output["result"] or {}).get("text") == canonical
    assert row["text"] == text


def test_selection_publishes_clean_text_and_a_closed_reference_pool(tmp_path):
    from test_selection import fixture_root, row

    from tts_data_pipeline.annotations.flexivoice_text import run
    from tts_data_pipeline.selection import plan, publish, resolve_duplicates, scan

    def sample(name, text, duration, speaker="p001", config="ears", audio=None):
        value = row(name, audio or name, text=text, language=None, duration=duration)
        value.update(
            source_config=config,
            metadata_json=json.dumps(
                {"upstream": {"wav_path": f"audio/instruction_data/ears/{speaker}/{name}.wav"}}
            ),
        )
        return value

    records = [
        sample("a", "hello [LAUGHTER]", 4, audio="same"),
        sample("b", "hello [Laughter]", 4, audio="same"),
        sample("c", "other [inaudible]", 4),
        sample("d", "short", 2),
        sample("e", "short", 2, "p002"),
        sample("f", "only long text", 4, "p002"),
        sample("held", "hello", 4, "p003"),
        sample("excluded", "hello", 4, config="emilia_psc"),
        sample("broken", "hello [GARBAGE]", 4),
        sample("borderline", "first reference", 3.01, "p004"),
        sample("other-long", "second reference", 4, "p004"),
    ]
    root, rules_path = fixture_root(tmp_path, {"flexivoice": records})
    annotation_path = run(root, tmp_path / "annotation")
    annotation = json.loads(annotation_path.read_text())
    rules = json.loads(
        (Path(__file__).parents[1] / "configs/selections/selection-nv-v1.json").read_text()
    )
    rules.pop("parent_selection")
    rules.update(
        source_priority=["flexivoice"],
        require_language=False,
        reference_policy="same_speaker_cross_utterance",
        duplicate_policy="keep_instruction_variants",
        flexivoice={
            "source_configs": ["ears"],
            "holdout_speakers": ["ears:p003"],
            "min_reference_seconds": 3,
        },
        sample_rate_policy={"flexivoice": {"min_inclusive": 16000}},
    )
    rules["reason_priority"] = [3004, *rules["reason_priority"], 5002]
    rules["annotation_inputs"] = [
        {
            "text_source": 1,
            "task": "flexivoice_text",
            "run_id": annotation["run_id"],
            "datasets": ["flexivoice"],
        }
    ]
    from tts_data_pipeline.selection import file_sha

    release = root / "datasets/flexivoice/v0.1"
    measured = lance.write_dataset(
        pa.table(
            {
                "target_id": [r["sample_id"] for r in records],
                "status": ["ok"] * len(records),
                "profile_id": ["measured"] * len(records),
                "processed_frames": [
                    int((2.99 if i == 9 else r["duration_seconds"]) * 48000)
                    for i, r in enumerate(records)
                ],
            }
        ),
        release / "measured.lance",
    )
    evidence_path = release / "measured.json"
    evidence_path.write_text(
        json.dumps(
            {
                "status": "complete",
                "kind": "dots_vae",
                "rows": len(records),
                "selection": {"mode": "all_samples"},
                "profile_id": "measured",
                "table_path": "measured.lance",
                "lance_version": measured.version,
                "inputs": [
                    {
                        "artifact_kind": "base",
                        "manifest_sha256": file_sha(release / "manifest.json"),
                    }
                ],
            }
        )
    )
    rules["flexivoice"]["reference_duration_manifest"] = str(evidence_path.relative_to(root))
    rules_path.write_text(json.dumps(rules))
    work = tmp_path / "selection"
    p = plan(root, work, rules_path)
    scan(work, workers=1)
    resolve_duplicates(work)
    manifest = publish(work, workers=1)
    assert manifest["reason_dictionary"]["version"] == 8
    assert manifest["outputs"][0]["selected_rows"] == 4
    output = manifest["outputs"][0]
    ds = lance.dataset(root / output["table_path"], version=output["base_version"])
    branch = ds.checkout_version((p["selection_id"], output["lance_version"]))
    results = {
        r["sample_id"]: r
        for r in branch.to_table(
            columns=["sample_id", "text", "selected_text", "selection_reason"]
        ).to_pylist()
    }
    assert results[records[0]["sample_id"]]["selected_text"] == "hello [Laughter]"
    assert results[records[2]["sample_id"]]["selected_text"] == "other"
    assert [results[r["sample_id"]]["selection_reason"] for r in records] == [
        0,
        0,
        0,
        0,
        5002,
        5002,
        7001,
        3004,
        1003,
        5002,
        5002,
    ]
    assert ds.to_table(columns=["text"])["text"].to_pylist() == [r["text"] for r in records]
