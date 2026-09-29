"""Check normative documents, generated Arrow types, examples, and optional deployed copy."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import _bootstrap  # noqa: F401
import pyarrow as pa
import yaml

from tts_data_pipeline.contract import contract_types, feature_targets_schema, schema_description
from tts_data_pipeline.feature_contract import (
    target_set_sha256,
    validate_feature_coverage,
    validate_profile_name,
    validate_speaker_mode,
)
from tts_data_pipeline.schema import digest


def check_selection_example(root):
    """Check example coherence, not production files or the global duplicate computation."""
    example = json.loads((root / "examples/selection-manifest.example.json").read_text())
    assert example["example_only"] and not example["runnable"]
    rules_bytes = (root / "examples/selection-rules.example.json").read_bytes()
    assert hashlib.sha256(rules_bytes).hexdigest() == example["rules"]["sha256"]
    rules = json.loads(rules_bytes)
    codes = {int(k) for k in example["reason_dictionary"]["codes"]}
    priority = example["reason_dictionary"]["priority"]
    assert priority == rules["reason_priority"]
    assert len(priority) == len(set(priority)) and set(priority) == codes - {0, 65535}
    assert all(0 <= int(bit) < 32 for bit in example["flag_dictionary"]["bits"])
    inputs = {(i["dataset_id"], i["release_id"]): i for i in example["inputs"]}
    outputs = {(i["dataset_id"], i["release_id"]): i for i in example["outputs"]}
    if len(inputs) != len(example["inputs"]) or len(outputs) != len(example["outputs"]):
        raise ValueError("Duplicate selection dataset")
    if inputs.keys() != outputs.keys():
        raise ValueError("Selection outputs must cover every input dataset")
    for key, output in outputs.items():
        source = inputs[key]
        assert source["branch"] is None and source["lance_version"] == output["base_version"]
        assert source["table_path"] == output["table_path"]
        assert output["branch"] == example["selection_id"]
        assert type(output["lance_version"]) is int and output["lance_version"] > 0
        counts = output["reason_counts"]
        if any(int(c) not in codes - {65535} for c in counts):
            raise ValueError("Unknown or pending selection reason")
        assert all(type(v) is int and v >= 0 for v in counts.values())
        assert sum(counts.values()) == output["rows"] == source["rows"]
        assert counts.get("0", 0) == output["selected_rows"]
        validation = output["validation"]
        assert validation["rows_checked"] == output["rows"] and validation["base_unchanged"]
        assert all(
            validation[k] == 0
            for k in ("null_reason", "null_flags", "unknown_reason", "unknown_flag_bits", "pending")
        )


def check_annotation_examples(root):
    example = json.loads((root / "examples/annotation-manifest.example.json").read_text())
    quality = json.loads((root / "examples/quality.example.json").read_text())
    output = example["outputs"][0]
    base = next(i for i in example["inputs"] if i["alias"] == output["base_input_alias"])
    assert base["branch"] is None and base["rows"] == output["table_rows"]
    assert output["table_path"] == base["table_path"] == quality["table_path"]
    assert output["storage_kind"] == quality["storage_kind"] == "sample_branch"
    assert output["branch"] == quality["branch"] == f"ann/{example['task']}/{example['run_id']}"
    assert type(output["lance_version"]) is int and output["lance_version"] > 0
    assert output["lance_version"] == quality["lance_version"]
    assert example["profile_id"] == digest(example["profile"])
    column = output["columns"][quality["column"]]
    selected = column["selection"]["target_ids"]
    assert selected == sorted(set(selected))
    assert (
        hashlib.sha256("".join(s + "\n" for s in selected).encode()).hexdigest()
        == (column["selection"]["target_set_sha256"])
    )
    coverage = column["coverage"]
    assert coverage["missing"] == 0
    counts = dict.fromkeys(("ok", "failed", "unsupported", "skipped"), 0)
    for row in quality["rows"]:
        result = row["value"]
        if row["sample_id"] not in selected:
            assert result is None
            continue
        assert result is not None and result["input_fingerprint"]
        counts[result["status"]] += 1
        if result["status"] == "ok":
            assert result["result"] is not None and result["error_code"] is None
        else:
            assert result["result"] is None and result["error_code"]
    assert sum(counts.values()) == coverage["total_targets"] == len(selected)
    assert all(coverage[k] == v for k, v in counts.items())
    ordered = "".join(row["sample_id"] + "\n" for row in quality["rows"])
    assert (
        hashlib.sha256(ordered.encode()).hexdigest()
        == (output["validation"]["ordered_sample_ids_sha256"])
    )
    revisions = json.loads((root / "examples/text-revision.example.json").read_text())
    for row in revisions["rows"]:
        replacement, source = row["selected_text"], row["selected_text_source"]
        assert (replacement is None) == (source is None)
        if replacement is not None:
            assert replacement.strip() and str(source) in revisions["text_sources"]
            assert row["annotation"]["status"] == "ok"
            assert row["annotation"]["result"] == {"action": "replace", "text": replacement}
        raw = row["base_text"] if replacement is None else replacement
        assert row["text_revision"] == digest(["selected-text-v1", raw, revisions["normalization"]])


def check(target=None):
    root = Path(__file__).resolve().parents[1] / "docs/data-contract"
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        if path.suffix == ".json":
            json.loads(
                path.read_text(),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"Nonfinite JSON: {value}")
                ),
            )
        elif path.suffix in {".yaml", ".yml"}:
            yaml.safe_load(path.read_text())
        elif path.suffix == ".md":
            for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" in link or link.startswith("#"):
                    continue
                if not (path.parent / link.split("#")[0]).exists():
                    raise ValueError(f"Broken link in {path}: {link}")
    assert json.loads((root / "schemas/arrow-schemas.json").read_text()) == contract_types()
    check_selection_example(root)
    check_annotation_examples(root)
    feature = json.loads((root / "examples/feature-manifest.example.json").read_text())
    subset = json.loads((root / "examples/feature-subset-manifest.example.json").read_text())
    for manifest in (feature, subset):
        validate_feature_coverage(manifest)
        assert (
            sum(manifest["error_counts"].values()) == manifest["rows"] - manifest["coverage"]["ok"]
        )
        assert (
            manifest["profile"]
            == json.loads((root / "examples/codec-profile.example.json").read_text())["profile"]
        )
    targets = json.loads((root / "examples/feature-targets.example.json").read_text())["rows"]
    assert pa.Table.from_pylist(targets, schema=feature_targets_schema()).to_pylist() == targets
    assert len(targets) == subset["selection"]["target_count"]
    assert target_set_sha256(targets) == subset["selection"]["target_set_sha256"]
    assert (
        digest(schema_description(feature_targets_schema()))
        == subset["selection"]["table"]["schema_sha256"]
    )
    samples = sorted(
        (
            {
                "target_kind": "sample",
                "target_id": digest(["sample", i]),
                "parent_sample_id": digest(["sample", i]),
            }
            for i in range(100)
        ),
        key=lambda row: row["target_id"],
    )
    assert target_set_sha256(samples) == feature["selection"]["target_set_sha256"]
    modes = json.loads((root / "examples/training-modes.example.json").read_text())
    assert modes["example_only"] and not modes["runnable"]
    for case in modes["cases"]:
        validate_speaker_mode(case["recipe"], case["record"])
    for name in ("codec-profile", "speaker-profile"):
        template = json.loads((root / f"examples/{name}.example.json").read_text())
        assert template["example_only"] and not template["runnable"]
        validate_profile_name(template.get("profile_name"))
    if target is not None:
        expected = {p.relative_to(root) for p in files}
        deployed = {
            p.relative_to(target)
            for name in ["README.md", "CONTRACT.md", "specs", "schemas", "examples"]
            for p in ([target / name] if (target / name).is_file() else (target / name).rglob("*"))
            if p.is_file()
        }
        if expected != deployed:
            raise ValueError(f"Contract file set differs: {expected ^ deployed}")
        for path in files:
            if path.read_bytes() != (target / path.relative_to(root)).read_bytes():
                raise ValueError(f"Deployed contract differs: {path.relative_to(root)}")
    print(
        f"{len(files)} contract files checked; Arrow descriptors and examples consistent"
        + ("; deployed copy byte-identical" if target else "")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path)
    check(parser.parse_args().target)
