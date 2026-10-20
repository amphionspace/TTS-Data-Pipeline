"""selection-v4 hifitts2: ASR text verified against a base text variant (1009) and speaker
count taken from base metadata (6002)."""

import json
import random

import lance
import pytest
from test_selection_filter import asr, row, write_annotation, write_base, write_parent
from test_selection_speaker import speaker_rules

from tts_data_pipeline.schema import digest
from tts_data_pipeline.selection import (
    comparable,
    edits_within,
    plan,
    publish,
    resolve_duplicates,
    scan,
    verify_text,
    write_json,
)

WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november "
    "oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu"
)
FOLD = {"colour": "color"}
VERIFICATION = {
    "reference": {"column": "text_variants", "kind": "source_normalized"},
    "fold_words": FOLD,
    "max_edit_ratio": "1/100",
}


def levenshtein(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def test_bounded_edit_distance_matches_levenshtein():
    rng = random.Random(0)
    for _ in range(3000):
        a = "".join(rng.choice("abc") for _ in range(rng.randint(0, 20)))
        b = list(a)
        for _ in range(rng.randint(0, 5)):
            i = rng.randint(0, len(b))
            if rng.random() < 0.5 or not b:
                b.insert(i, rng.choice("abc"))
            else:
                b.pop(min(i, len(b) - 1))
        b, k = "".join(b), rng.randint(0, 4)
        exact = levenshtein(a, b)
        assert edits_within(a, b, k) == (exact if exact <= k else k + 1)


def test_comparable_form_ignores_case_punctuation_spacing_and_listed_spellings():
    assert comparable("To-morrow, Mr. Café’s colour!", FOLD) == "tomorrowmrcafescolor"
    assert comparable("tomorrow mr cafes color", FOLD) == "tomorrowmrcafescolor"


def test_verification_limit_scales_with_reference_length():
    variants = [{"kind": "source_normalized", "text": WORDS}]
    length = len(comparable(WORDS, FOLD))
    assert length // 100 == 1  # one edit allowed, two are not
    assert verify_text(WORDS.title() + ".", variants, VERIFICATION) == "agree"
    assert verify_text(WORDS.replace("zulu", "zula"), variants, VERIFICATION) == "agree"
    assert verify_text(WORDS.replace("zulu", "zola"), variants, VERIFICATION) == "disagree"
    assert verify_text(WORDS, [], VERIFICATION) == "no_reference"
    assert verify_text(WORDS, [{"kind": "other", "text": WORDS}], VERIFICATION) == "no_reference"
    assert verify_text(WORDS, [{"kind": "source_normalized", "text": "..."}], VERIFICATION) == (
        "no_reference"
    )


def verification_rules(**changes):
    configuration = speaker_rules(
        speaker_inputs=[],
        speaker_metadata=[
            {"datasets": ["alpha"], "path": ["parquet_fields", "speaker_count"], "speaker_count": 1}
        ],
    )
    del configuration["speaker_inputs"]
    configuration["text_sources"]["1"]["verification"] = json.loads(json.dumps(VERIFICATION))
    priority = configuration["reason_priority"]
    priority.insert(priority.index(1008) + 1, 1009)
    configuration.update(changes)
    return configuration


def record(name, asr_text, reference, speakers=1):
    value = row(name, text=WORDS.lower(), duration=10.0)
    value["text_variants"] = (
        [{"kind": "source_normalized", "text": reference, "language": "en"}]
        if reference is not None
        else []
    )
    fields = {} if speakers is None else {"speaker_count": speakers}
    value["metadata_json"] = json.dumps({"parquet_fields": fields})
    return value, asr_text


def test_hifitts2_style_rows_need_text_agreement_and_one_upstream_speaker(tmp_path):
    root = tmp_path / "unified"
    cases = [
        record("agree", WORDS.capitalize() + ".", WORDS),
        record("one-edit", WORDS.replace("zulu", "zula"), WORDS),
        record("two-edits", WORDS.replace("zulu", "zola"), WORDS),
        record("no-variant", WORDS, None),
        record("dialog", WORDS, WORDS, speakers=2),
        record("no-count", WORDS, WORDS, speakers=None),
        record("bool-count", WORDS, WORDS, speakers=True),
        record("asr-failed", None, WORDS),
    ]
    records = [r for r, _ in cases]
    base = write_base(root, "alpha", records)
    results = [asr(r, text=t) if t is not None else asr(r, status="failed") for r, t in cases]
    write_annotation(root, "asr-run", base, records, results)
    write_parent(root, [])
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, verification_rules())
    work = tmp_path / "scratch"
    p = plan(root, work, rules_path, selection_id="v4")
    assert "speaker_inputs" not in p
    scan(work, workers=2)
    resolve_duplicates(work)
    manifest = json.loads(json.dumps(publish(work, workers=2)))
    (output,) = manifest["outputs"]
    ds = lance.dataset(root / output["table_path"], version=output["base_version"])
    branch = ds.checkout_version((output["branch"], output["lance_version"]))
    table = branch.to_table(columns=["sample_id", "selection_reason", "selected_text"])
    reasons = dict(zip(table["sample_id"].to_pylist(), table["selection_reason"].to_pylist()))
    assert reasons == {
        digest("agree"): 0,
        digest("one-edit"): 0,
        digest("two-edits"): 1009,
        digest("no-variant"): 1009,
        digest("dialog"): 6002,
        digest("no-count"): 6002,
        digest("bool-count"): 6002,
        digest("asr-failed"): 1003,
    }
    # The selected text is the ASR text, not the reference it was checked against.
    texts = dict(zip(table["sample_id"].to_pylist(), table["selected_text"].to_pylist()))
    assert texts[digest("agree")] == WORDS.capitalize() + "."
    assert manifest["reason_dictionary"]["version"] == 6
    assert manifest["reason_dictionary"]["codes"]["1009"] == "text_disagreement"
    assert manifest["reason_dictionary"]["codes"]["6002"] == "not_single_speaker"
    assert manifest["text_sources"]["1"]["verification"] == VERIFICATION
    assert manifest["coverage"]["text_verification"]["outcomes"] == {
        "agree": 5,
        "disagree": 1,
        "no_reference": 1,
        "unchecked": 1,
    }
    speaker = manifest["coverage"]["speaker_structure"]
    assert speaker["inputs"] == [
        {
            "dataset_id": "alpha",
            "source": "base metadata_json",
            "path": ["parquet_fields", "speaker_count"],
            "speaker_count": 1,
        }
    ]
    assert speaker["outcomes"] == {"passed": 5, "count_2": 1, "unavailable": 2}
    assert manifest["speaker_metadata"][0]["datasets"] == ["alpha"]
    assert "speaker_inputs" not in manifest


def test_verification_needs_its_reason_code(tmp_path):
    configuration = verification_rules()
    configuration["reason_priority"].remove(1009)
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, configuration)
    with pytest.raises(ValueError, match="1009"):
        plan(tmp_path / "unified", tmp_path / "scratch", rules_path, selection_id="x")


def test_a_dataset_takes_speaker_evidence_from_one_place_only(tmp_path):
    configuration = verification_rules()
    configuration["speaker_inputs"] = [
        {"task": "speaker_structure", "run_id": "moss", "datasets": ["alpha"], "speaker_count": 1}
    ]
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, configuration)
    with pytest.raises(ValueError, match="exactly once"):
        plan(tmp_path / "unified", tmp_path / "scratch", rules_path, selection_id="x")


def test_verification_rules_must_name_the_supported_reference(tmp_path):
    configuration = verification_rules()
    configuration["text_sources"]["1"]["verification"]["reference"]["column"] = "text"
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, configuration)
    with pytest.raises(ValueError, match="verification reference"):
        plan(tmp_path / "unified", tmp_path / "scratch", rules_path, selection_id="x")
