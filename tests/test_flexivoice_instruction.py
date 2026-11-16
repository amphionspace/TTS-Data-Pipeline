import json
from pathlib import Path

import lance
import pyarrow as pa


def test_instruction_revision_flows_into_the_selection_branch(tmp_path):
    from test_selection import fixture_root, row

    from tts_data_pipeline.annotations import flexivoice_instruction as fi
    from tts_data_pipeline.annotations.flexivoice_text import run as text_run
    from tts_data_pipeline.selection import file_sha, plan, publish, resolve_duplicates, scan

    def sample(name, instruction, speaker, config="ears", duration=4):
        value = row(name, name, text=f"text {name}", language=None, duration=duration)
        folder = "ears" if config == "ears" else "expresso/audio_48khz/read/ex01"
        value.update(
            source_config=config,
            metadata_json=json.dumps(
                {
                    "instruction": instruction,
                    "upstream": {"wav_path": f"audio/instruction_data/{folder}/{speaker}/{name}.wav"},
                }
            ),
        )
        return value

    records = [
        sample("s1_01_whisper", "A male American whispers.", "p001"),
        sample("s2_01_regular", "A female voice, neutral.", "p001"),
        sample("s3_01_fast", "A husky man speaks.", "p002"),  # revision still names the speaker
        sample("s4_01_slow", "Slow.", "p002"),  # revision keeps no style
        sample("w1", "Whisper this sentence", "user1", config="WHSP"),
        sample("w2", "Say it softly", "user1", config="WHSP"),
    ]
    root, rules_path = fixture_root(tmp_path, {"flexivoice": records})
    text_manifest = json.loads(text_run(root, tmp_path / "text").read_text())

    work = tmp_path / "instruction"
    fi.collect(root, work, {"ears"})
    units = [json.loads(line) for line in (work / "instructions.jsonl").open()]
    styles = {
        "whisper": "Whisper the line softly.",
        "regular": "Read it in a natural, neutral way.",
        "fast": "Speak fast like a husky man.",
        "slow": "",
    }
    with (work / "generations.jsonl").open("w") as out:
        for unit in units:
            style = styles[unit["recording_style"]]
            out.write(json.dumps({"sha256": unit["sha256"], "style": style, "removed": ""}) + "\n")
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    instruction_manifest = json.loads(fi.publish(root, work, model).read_text())
    assert instruction_manifest["outputs"][0]["table"]["rows"] == len(records)

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
            "source_configs": ["ears", "WHSP"],
            "holdout_speakers": [],
            "min_reference_seconds": 3,
        },
        sample_rate_policy={"flexivoice": {"min_inclusive": 16000}},
    )
    rules["reason_priority"] = [3004, 3005, *rules["reason_priority"], 5002]
    rules["annotation_inputs"] = [
        {"text_source": 1, "task": "flexivoice_text", "run_id": text_manifest["run_id"],
         "datasets": ["flexivoice"]}
    ]
    rules["instruction_inputs"] = [
        {"task": "flexivoice_instruction", "run_id": instruction_manifest["run_id"],
         "datasets": ["flexivoice"], "source_configs": ["ears"]}
    ]
    release = root / "datasets/flexivoice/v0.1"
    measured = lance.write_dataset(
        pa.table(
            {
                "target_id": [r["sample_id"] for r in records],
                "status": ["ok"] * len(records),
                "profile_id": ["measured"] * len(records),
                "processed_frames": [int(r["duration_seconds"] * 48000) for r in records],
            }
        ),
        release / "measured.lance",
    )
    evidence = release / "measured.json"
    evidence.write_text(
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
                    {"artifact_kind": "base", "manifest_sha256": file_sha(release / "manifest.json")}
                ],
            }
        )
    )
    rules["flexivoice"]["reference_duration_manifest"] = str(evidence.relative_to(root))
    rules_path.write_text(json.dumps(rules))
    selection_work = tmp_path / "selection"
    p = plan(root, selection_work, rules_path)
    scan(selection_work, workers=1)
    resolve_duplicates(selection_work)
    manifest = publish(selection_work, workers=1)
    assert manifest["reason_dictionary"]["version"] == 10
    assert manifest["reason_dictionary"]["codes"][3005] == "instruction_unavailable"
    assert manifest["instruction_inputs"][0]["run_id"] == instruction_manifest["run_id"]
    output = manifest["outputs"][0]
    ds = lance.dataset(root / output["table_path"], version=output["base_version"])
    branch = ds.checkout_version((p["selection_id"], output["lance_version"]))
    results = {
        r["sample_id"]: r
        for r in branch.to_table(
            columns=["sample_id", "selection_reason", "selected_instruction"]
        ).to_pylist()
    }
    by_name = {r["sample_id"]: name for r, name in zip(records, [
        "s1", "s2", "s3", "s4", "w1", "w2"], strict=True)}
    got = {by_name[k]: (v["selection_reason"], v["selected_instruction"]) for k, v in results.items()}
    assert got["s1"] == (0, "Whisper the line softly.")
    assert got["s2"] == (0, "Read it in a natural, neutral way.")
    assert got["s3"] == (3005, None)  # still names the speaker
    assert got["s4"] == (3005, None)  # no style left
    assert got["w1"] == (0, None) and got["w2"] == (0, None)  # untouched subset keeps its own
