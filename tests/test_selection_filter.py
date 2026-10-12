"""selection-v2 rules: ASR text sources, strict language, text policy, timeline evidence."""

import copy
import csv
import hashlib
import json
import math
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.schema import digest
from tts_data_pipeline.selection import (
    classify,
    effective_characters,
    file_sha,
    plan,
    publish,
    repetition,
    resolve_duplicates,
    scan,
    write_json,
)
from tts_data_pipeline.selection import publish as publish_selection

REPOSITORY = Path(__file__).resolve().parents[1]


def rules():
    return json.loads((REPOSITORY / "configs/selections/selection-v2.json").read_text())


def row(name, audio=None, text="hello world", language="en", duration=2.0):
    return {
        "sample_id": digest(name),
        "audio_sha256": digest(audio or name),
        "text": text,
        "language": language,
        "num_frames": round(duration * 16000),
        "sample_rate": 16000,
        "duration_seconds": round(duration * 16000) / 16000,
        "parent_sample_id": None,
        "segment_start_frame": None,
        "segment_end_frame": None,
    }


def asr(base, text="hello world", label="English", status="ok", **changes):
    value = {
        "sample_id": base["sample_id"],
        "status": status,
        "error_code": None if status == "ok" else "asr_output_truncated",
        "audio_sha256": base["audio_sha256"] if status == "ok" else None,
        "source_language": base["language"] if status == "ok" else None,
        "text": text if status == "ok" else None,
        "language": label if status == "ok" else None,
        "language_mismatch": False if status == "ok" else None,
        "candidate_text": None,
        "candidate_response": None,
        "text_source": 1,
    }
    return value | changes


def decide(value, annotation=None, exclusions=None):
    return classify(value, "alpha", rules(), set(), exclusions or {}, annotation)[0]


def test_effective_characters_are_letters_and_numbers_only():
    assert effective_characters("Hi, 你好！ 42…") == "Hi你好42"
    assert effective_characters("…!? ") == ""
    assert decide(row("p", text="…!?")) == 1001


@pytest.mark.parametrize(
    "language,text,path",
    [
        ("en", " ".join(["a b c d"] * 5), "word"),
        ("en", "too short to check", None),
        ("zh", "你好" * 15, "char"),
        ("zh", "你好" * 14, None),
    ],
)
def test_repetition_paths_and_minimum_lengths(language, text, path):
    actual, coverage = repetition(text, language, rules()["text_filter"]["repetition"])
    assert actual == path
    assert math.isnan(coverage) if path is None else coverage == 1.0


def test_repetition_needs_both_count_and_coverage():
    config = rules()["text_filter"]["repetition"]
    looped = " ".join(["one two three four"] * 5)
    assert repetition(looped, "en", config) == ("word", 1.0)
    # Five occurrences covering 20 of 30 words stay below 80% coverage.
    padded = looped + " " + " ".join(f"w{i}" for i in range(10))
    assert repetition(padded, "en", config)[1] == pytest.approx(20 / 30)
    assert decide(row("loop", text=looped, duration=8)) == 1004
    assert decide(row("pad", text=padded, duration=10)) == 0


def distinct(count, start):
    """Non-repeating letters, so only the length ratio can trigger."""
    return "".join(chr(start + i) for i in range(count))


@pytest.mark.parametrize(
    "language,count,duration,expected",
    [
        ("zh", 50, 2.0, 0),  # exactly 0.04 s/char passes
        ("zh", 51, 2.0, 1005),
        ("zh", 1, 2.0, 0),  # exactly 2.0 s/char passes
        ("zh", 1, 2.001, 1006),
        ("en", 100, 1.5, 0),  # exactly 0.015 s/char passes
        ("en", 101, 1.5, 1005),
        ("en", 2, 2.0, 0),  # exactly 1.0 s/char passes
        ("en", 2, 2.01, 1006),
    ],
)
def test_seconds_per_character_bounds_are_inclusive(language, count, duration, expected):
    text = distinct(count, 0x4E00 if language == "zh" else 0x100)
    assert decide(row("r", text=text, language=language, duration=duration)) == expected


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, 0),
        ({"language": "Chinese"}, 2003),
        ({"language": ""}, 2003),
        ({"language": "English,Chinese"}, 2003),
        ({"language_mismatch": True}, 2003),
        ({"candidate_response": '{"error_code": "x"}'}, 2003),
        ({"candidate_text": ""}, 2003),
    ],
)
def test_strict_asr_language(changes, expected):
    base = row("game", text="原文", language="en-US")
    assert decide(base, asr(base) | changes) == expected


def test_asr_failure_never_falls_back_to_base_text():
    base = row("game", text="a perfectly good subtitle")
    reason, flags, _, text = classify(base, "alpha", rules(), set(), {}, asr(base, status="failed"))
    assert (reason, text) == (1003, "")
    assert flags & (1 << 1)
    assert decide(base, asr(base, text="  ")) == 1001


@pytest.mark.parametrize(
    "text,language",
    [
        ("おはよう<ハ>", "ja"),
        ("inquir}^ more", "en"),
        ("[Sidenote: x] text", "en"),
        ("g {\\displaystyle g", "en"),
    ],
)
def test_markup_characters_exclude(text, language):
    assert decide(row("m", text=text, language=language, duration=1.5)) == 1007


@pytest.mark.parametrize(
    "language,text,expected",
    [
        ("zh", "cubic的系数是a。", 0),  # code-switching keeps a native majority
        ("zh", "We grow from eighteen people to thirty.", 2004),  # English audio labelled zh
        ("zh", "トミカラコンラブラ。", 2004),
        ("ja", "HIAセンターか。", 0),
        ("ja", "ＯＫＡＭＡＢＡＲ……？", 2004),
        ("ko", "I N F P는 아니야.", 0),  # exactly half passes
        ("en", "언제나 내 안에 항상.", 2004),
        ("pt", "для заказа", 2004),
        ("fr", "Ça va très bien, merci.", 0),
        ("en", "2024 42", 0),  # no letters: not judged here
    ],
)
def test_text_script_must_match_language(language, text, expected):
    assert decide(row("s", text=text, language=language, duration=1.5)) == expected


def test_asr_line_break_marks_chunk_join_but_base_text_does_not():
    base = row("chunk", text="kept subtitle words", duration=3)
    assert decide(base, asr(base, text="first part.\nsecond part")) == 1008
    assert decide(row("b", text="first part.\nsecond part", duration=3)) == 0


def test_asr_text_supersedes_base_text_in_duplicate_groups():
    import numpy as np

    from tts_data_pipeline.selection import ROW, decide_group

    def member(dataset, name, text, source, language=1):
        value = np.zeros((), dtype=ROW)
        value["sample_id"] = digest(name).encode()
        value["text_hash"] = digest(text)[:32].encode()
        value["language"], value["text_source"] = language, source
        return dataset, 0, value

    base_first = member(0, "a", "old upstream text", 0)
    annotated = member(1, "b", "new asr text", 2)
    winner, conflict = decide_group([base_first, annotated])
    assert not conflict and winner[0] == 1
    # Language disagreement is never resolved by text source.
    winner, conflict = decide_group([member(0, "a", "x", 0, 2), annotated])
    assert conflict and winner is None
    # Two different ASR texts for the same bytes stay an unresolved conflict.
    assert decide_group([annotated, member(2, "c", "other asr text", 1)])[1]


def test_asr_identity_must_match_base():
    base = row("game")
    with pytest.raises(ValueError, match="identity"):
        decide(base, asr(base, audio_sha256=digest("other")))
    with pytest.raises(ValueError, match="aligned"):
        decide(base, asr(base, sample_id=digest("other")))


def test_timeline_exclusion_combines_with_other_sample_exclusions():
    base = row("t")
    timeline = {"reason": 105, "scope": {"sample_id": base["sample_id"]}}
    reason, flags, _, _ = classify(
        base, "alpha", rules(), set(), {("alpha", base["sample_id"]): [timeline]}
    )
    assert reason == 105 and flags & (1 << 10)


# ---------------------------------------------------------------- end-to-end


def write_base(root, name, records):
    release = root / "datasets" / name / "v0.1"
    release.mkdir(parents=True)
    ds = lance.write_dataset(
        pa.Table.from_pylist(records),
        release / "samples.lance",
        data_storage_version="2.2",
        max_rows_per_file=2,
    )
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
    return {
        "dataset_id": name,
        "manifest_sha256": file_sha(release / "manifest.json"),
        "lance_version": ds.version,
    }


def write_annotation(root, run_id, base, records, results):
    path = Path("datasets") / base["dataset_id"] / "v0.1/annotations/transcription" / run_id
    # Deliberately not in base order: alignment must join by sample_id.
    rows = [
        {
            "sample_id": r["sample_id"],
            "input_fingerprint": "x",
            "status": a["status"],
            "error_code": a["error_code"],
            "result": {
                "audio_sha256": a["audio_sha256"],
                "source_language": a["source_language"],
                "text": a["text"],
                "language": a["language"],
                "language_mismatch": a["language_mismatch"],
                "language_candidate_text": a["candidate_text"],
                "language_candidate_response_json": a["candidate_response"],
            },
        }
        for r, a in reversed(list(zip(records, results, strict=True)))
    ]
    ds = lance.write_dataset(pa.Table.from_pylist(rows), root / path / "results.lance")
    folder = root / "annotations/transcription" / run_id
    folder.mkdir(parents=True)
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "run_id": run_id,
            "profile_id": "profile",
            "inputs": [base],
            "outputs": [
                {
                    "dataset_id": base["dataset_id"],
                    "table": {
                        "table_path": str(path / "results.lance"),
                        "branch": None,
                        "lance_version": ds.version,
                        "rows": len(rows),
                    },
                }
            ],
        },
    )


def write_parent(root, exclusions):
    folder = root / "selections" / "parent"
    folder.mkdir(parents=True)
    path = folder / "exclusions.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in exclusions))
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "selection_id": "parent",
            "exclusions": {
                "effective_path": path.name,
                "effective_sha256": file_sha(path),
                "effective_count": len(exclusions),
            },
        },
    )


def nul_ending_text():
    """A text whose SHA256 ends in 0x00, which numpy S32 elements drop when read as bytes."""
    return next(
        t
        for t in (f"plain words {i}" for i in range(100000))
        if hashlib.sha256(t.encode()).digest()[-1] == 0
    )


def test_inherited_annotated_selection_publishes_audited_text(tmp_path):
    root = tmp_path / "unified"
    game = [
        row("g0", text="#{NICKNAME}, hi", language="en-US"),
        row("g1", text="kept subtitle"),
        row("g2", text="fine"),
        row("g3", audio="shared", text="same words here"),
    ]
    other = [
        row("o0", audio="shared", text="same words here"),
        row("o1", text="audited"),
        row("o2", text=nul_ending_text()),
    ]
    game_base = write_base(root, "game", game)
    write_base(root, "other", other)
    results = [
        asr(game[0], text=" Traveler, hi "),
        asr(game[1], status="failed"),
        asr(game[2], label="Chinese"),
        asr(game[3], text="same words here"),
    ]
    write_annotation(root, "asr-run", game_base, game, results)
    wuthering = {
        "exclusion_id": digest("old"),
        "scope": {"audio_sha256": digest("never-present")},
        "reason": 103,
    }
    write_parent(root, [wuthering])
    evidence = tmp_path / "findings.tsv"
    with evidence.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["dataset", "sample_id", "findings"])
        writer.writerow(["other", digest("o1"), "source_duration_differs_gt50ms"])
        writer.writerow(["other", digest("o0"), "invalid_word_timestamps"])
    configuration = rules()
    configuration.update(
        parent_selection="parent",
        source_priority=["game", "other"],
        annotation_inputs=[
            {"text_source": 1, "task": "transcription", "run_id": "asr-run", "datasets": ["game"]}
        ],
        source_timeline_evidence={
            "path": str(evidence),
            "sha256": file_sha(evidence),
            "ignore_findings": ["invalid_word_timestamps"],
            "expected_rows": 1,
        },
    )
    configuration["text_sources"] = {"1": configuration["text_sources"]["1"]}
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, configuration)
    work = tmp_path / "scratch"
    p = plan(root, work, rules_path, selection_id="child")
    assert len(p["exclusions"]) == 2 and len(p["exclusion_additions"]) == 1
    scan(work, workers=2)
    resolve_duplicates(work)
    manifest = publish(work, workers=2)
    outcomes = {}
    for output in manifest["outputs"]:
        ds = lance.dataset(root / output["table_path"], version=output["base_version"])
        branch = ds.checkout_version((output["branch"], output["lance_version"]))
        outcomes.update({r["sample_id"]: r for r in branch.to_table().to_pylist()})
    g0 = outcomes[digest("g0")]
    assert (g0["selection_reason"], g0["selected_text"], g0["selected_text_source"]) == (
        0,
        "Traveler, hi",
        1,
    )
    g1 = outcomes[digest("g1")]
    assert (g1["selection_reason"], g1["selected_text"]) == (1003, "")
    assert outcomes[digest("g2")]["selection_reason"] == 2003
    # Identical audio and identical selected text: the higher-priority source represents.
    assert outcomes[digest("g3")]["selection_reason"] == 0
    assert outcomes[digest("g3")]["selected_text"] is None
    assert outcomes[digest("o0")]["selection_reason"] == 4001
    assert outcomes[digest("o1")]["selection_reason"] == 105
    assert outcomes[digest("o2")]["selection_reason"] == 0
    assert manifest["exclusions"]["parent"]["selection_id"] == "parent"
    assert manifest["exclusions"]["added"] == 1 and manifest["exclusions"]["effective_count"] == 2
    assert manifest["text_sources"]["1"]["datasets"] == ["game"]
    assert manifest["annotation_inputs"][0]["run_id"] == "asr-run"
    assert manifest["reason_dictionary"]["version"] == 2
    with pytest.raises(ValueError, match="already published"):
        publish_selection(work, workers=1)


def test_annotation_on_a_different_base_is_rejected(tmp_path):
    root = tmp_path / "unified"
    records = [row("g0")]
    base = write_base(root, "game", records)
    write_annotation(
        root, "asr-run", base | {"manifest_sha256": "0" * 64}, records, [asr(records[0])]
    )
    configuration = copy.deepcopy(rules())
    configuration.update(
        parent_selection=None,
        source_priority=["game"],
        annotation_inputs=[
            {"text_source": 1, "task": "transcription", "run_id": "asr-run", "datasets": ["game"]}
        ],
        source_timeline_evidence=None,
    )
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, configuration)
    with pytest.raises(ValueError, match="different base"):
        plan(root, tmp_path / "scratch", rules_path, selection_id="x")
