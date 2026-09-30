"""Regression checks for feature identity, selection coverage and conditional inputs."""

import copy
import hashlib
import json
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pytest

from tts_data_pipeline.contract import feature_targets_schema
from tts_data_pipeline.feature_contract import (
    equivalent_payloads,
    make_feature_run_id,
    target_set_sha256,
    validate_feature_coverage,
    validate_speaker_mode,
)
from tts_data_pipeline.timestamps import parse_timestamp

EXAMPLES = Path(__file__).resolve().parents[1] / "docs/data-contract/examples"


def example(name):
    return json.loads((EXAMPLES / f"{name}.example.json").read_text())


@pytest.mark.parametrize("value", [None, "", "   ", "name/escape", "name+unsafe"])
def test_profile_name_is_required(value):
    manifest = example("feature-manifest")
    if value is None:
        del manifest["profile_name"]
    else:
        manifest["profile_name"] = value
    with pytest.raises(ValueError, match="profile_name"):
        validate_feature_coverage(manifest)


def test_run_name_preserves_identity_and_uses_beijing_time():
    manifest = example("feature-manifest")
    run = make_feature_run_id(
        "example_synthetic", "codec", manifest["profile_name"], "2026-09-29T00:00:00Z"
    )
    assert run == manifest["run_id"]
    assert run.endswith("20260929T080000bjt-01") and "+" not in run
    manifest["run_id"] = run.replace("example_synthetic", "example-synthetic")
    with pytest.raises(ValueError, match="preserve"):
        validate_feature_coverage(manifest)


def test_timestamp_order_is_absolute_not_lexicographic():
    earlier, later = "2026-09-29T08:00:00+08:00", "2026-09-29T01:00:00Z"
    assert earlier > later
    assert parse_timestamp(earlier) < parse_timestamp(later)
    assert parse_timestamp(earlier) == parse_timestamp("2026-09-29T00:00:00+00:00")
    with pytest.raises(ValueError, match="timezone"):
        parse_timestamp("2026-09-29T08:00:00")


@pytest.mark.parametrize("value", ["2026-09-29T08:00:00", "2026-09-29T00:00:00Z"])
def test_new_feature_requires_explicit_beijing_timestamp(value):
    manifest = example("feature-manifest")
    manifest["finished_at"] = value
    with pytest.raises(ValueError):
        validate_feature_coverage(manifest)


def test_float_equivalence_does_not_imply_same_artifact():
    original = np.array([0.125, -0.25, 0.5], dtype="<f4")
    candidate = original.copy()
    candidate[0] = np.nextafter(candidate[0], np.float32(np.inf))
    assert (
        hashlib.sha256(original.tobytes()).digest() != hashlib.sha256(candidate.tobytes()).digest()
    )
    assert equivalent_payloads("speaker_embedding", original, candidate, atol=1e-6, rtol=1e-6)
    assert not equivalent_payloads("speaker_embedding", original, candidate)
    candidate[0] += 0.01
    assert not equivalent_payloads("speaker_embedding", original, candidate, atol=1e-6, rtol=1e-6)
    assert not equivalent_payloads("speaker_embedding", original, [np.nan, 0, 0], atol=1e-6)
    # Canonical, not the candidate, is the relative tolerance reference.
    assert not equivalent_payloads("speaker_embedding", [1.0], [2.0], rtol=0.5)
    assert equivalent_payloads("speaker_embedding", [2.0], [1.0], rtol=0.5)


def test_codec_comparison_is_discrete():
    assert equivalent_payloads("codec", [[1, 2]], [[1, 2]])
    assert not equivalent_payloads("codec", [[1, 2]], [[1, 3]])
    assert not equivalent_payloads("codec", [[1, 2]], [[1.0, 2.0]])
    with pytest.raises(ValueError, match="exact"):
        equivalent_payloads("codec", [[1]], [[1]], atol=1e-6)


def test_sample_coverage_is_bounded_by_target_snapshot():
    manifest = example("feature-subset-manifest")
    validate_feature_coverage(manifest)
    manifest["selection"].update(mode="all_samples", target_count=100)
    del manifest["selection"]["table"]
    manifest["rows"] = 100
    manifest["coverage"].update(total_targets=100, ok=100)
    validate_feature_coverage(manifest)
    manifest["selection"]["available_target_rows"] = 99
    with pytest.raises(ValueError, match="subset"):
        validate_feature_coverage(manifest)


@pytest.mark.parametrize(
    "mutation", ["missing", "wrong_total", "wrong_snapshot", "wrong_subset_rows", "empty"]
)
def test_invalid_selection_accounting(mutation):
    manifest = example("feature-subset-manifest")
    if mutation == "missing":
        manifest["coverage"]["missing"] = 1
    elif mutation == "wrong_total":
        manifest["coverage"]["ok"] = 1
    elif mutation == "wrong_snapshot":
        manifest["selection"]["table"]["lance_version"] = 0
    elif mutation == "wrong_subset_rows":
        manifest["selection"]["table"]["rows"] = 1
    else:
        manifest["selection"]["target_count"] = 0
    with pytest.raises(ValueError):
        validate_feature_coverage(manifest)


def test_subset_lance_preserves_samples_and_pinned_selection(tmp_path):
    rows = example("feature-targets")["rows"]
    manifest = example("feature-subset-manifest")
    schema = feature_targets_schema()
    assert all(not field.nullable for field in schema)
    path = tmp_path / "targets.lance"
    ds = lance.write_dataset(pa.Table.from_pylist(rows, schema=schema), path)
    ds.create_scalar_index("target_id", "BTREE")
    version = ds.version
    assert all(row["parent_sample_id"] == row["target_id"] for row in rows)
    actual = sorted(ds.to_table().to_pylist(), key=lambda row: row["target_id"])
    assert target_set_sha256(actual) == manifest["selection"]["target_set_sha256"]
    assert ds.to_table(filter=f"target_id = '{rows[0]['target_id']}'").to_pylist() == [rows[0]]
    extra = dict(rows[0], target_id="f" * 64, parent_sample_id="f" * 64)
    lance.write_dataset(pa.Table.from_pylist([extra], schema=schema), path, mode="append")
    pinned = sorted(
        lance.dataset(path, version=version).to_table().to_pylist(),
        key=lambda row: row["target_id"],
    )
    assert target_set_sha256(pinned) == manifest["selection"]["target_set_sha256"]
    with pytest.raises(ValueError, match="unique"):
        target_set_sha256([rows[0], rows[0]])
    with pytest.raises(ValueError, match="sorted"):
        target_set_sha256(reversed(rows))


@pytest.mark.parametrize("case", example("training-modes")["cases"], ids=lambda case: case["name"])
def test_valid_conditioning_modes(case):
    validate_speaker_mode(case["recipe"], case["record"])


@pytest.mark.parametrize(
    "mutation",
    [
        "trainable_cache",
        "missing_audio",
        "stale_embedding",
        "missing_codes",
        "self_flags",
        "wrong_codec_flag",
        "missing_identity",
    ],
)
def test_invalid_conditioning_modes(mutation):
    cases = {case["name"]: copy.deepcopy(case) for case in example("training-modes")["cases"]}
    case = cases["frozen_speaker_only"]
    if mutation == "trainable_cache":
        case["recipe"]["speaker_encoder_trainable"] = True
    elif mutation in {"missing_audio", "stale_embedding"}:
        case = cases["online_speaker_only"]
        if mutation == "missing_audio":
            del case["record"]["reference_audio"]
        else:
            case["record"]["reference_embeddings"] = [{"embedding": [0.1]}]
    elif mutation == "missing_codes":
        case = cases["frozen_icl"]
        del case["record"]["reference_codes"]
    elif mutation == "self_flags":
        case["recipe"]["reference_policy"] = "self"
    elif mutation == "wrong_codec_flag":
        case["recipe"]["require_reference_codec"] = True
    else:
        del case["record"]["ordered_reference_ids"]
    with pytest.raises(ValueError):
        validate_speaker_mode(case["recipe"], case["record"])


def test_selection_branch_requires_pinned_manifest_and_exact_filter():
    manifest = example("feature-manifest")
    manifest["selection"].update(
        mode="selection_branch",
        filter="selection_reason = 0",
        manifest_path="selections/example/manifest.json",
        manifest_sha256="a" * 64,
    )
    manifest["inputs"][0]["branch"] = "example"
    validate_feature_coverage(manifest)
    for field, value in [("branch", None), ("lance_version", None), ("lance_version", True)]:
        invalid = copy.deepcopy(manifest)
        invalid["inputs"][0][field] = value
        with pytest.raises(ValueError, match="must pin"):
            validate_feature_coverage(invalid)
    for field, value in [("filter", "selection_reason < 2"), ("manifest_sha256", "")]:
        invalid = copy.deepcopy(manifest)
        invalid["selection"][field] = value
        with pytest.raises(ValueError, match="must pin"):
            validate_feature_coverage(invalid)
