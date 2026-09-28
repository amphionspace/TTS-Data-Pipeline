"""Check normative documents, generated Arrow types, examples, and optional deployed copy."""

import argparse
import json
import re
from pathlib import Path

import _bootstrap  # noqa: F401
import yaml

from tts_data_pipeline.contract import contract_types


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
