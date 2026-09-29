"""Check normative documents, generated Arrow types, examples, and optional deployed copy."""

import argparse
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
    validate_speaker_mode,
)
from tts_data_pipeline.schema import digest


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
    example = json.loads((root / "examples/annotation-manifest.example.json").read_text())
    coverage = example["coverage"]
    assert (
        sum(coverage[k] for k in ("ok", "failed", "unsupported", "skipped", "missing"))
        == coverage["total_targets"]
    )
    assert example["rows"] == coverage["total_targets"] - coverage["missing"]
    assert example["table_rows"] >= coverage["total_targets"]
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
