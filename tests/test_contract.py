import json
from pathlib import Path

from tts_data_pipeline.contract import contract_types, schema_description
from tts_data_pipeline.convert import convert
from tts_data_pipeline.schema import VERSION, base_schema, digest
from tts_data_pipeline.writer import release_dataset


def test_checked_in_types_match_code():
    path = Path(__file__).resolve().parents[1] / "docs/data-contract/schemas/arrow-schemas.json"
    assert json.loads(path.read_text()) == contract_types()
    assert VERSION == "v0.1"


def test_release_manifest_pins_contract_and_snapshot(tmp_path):
    from test_convert import corpus

    manifest = convert("csemotions", corpus(tmp_path), tmp_path / "v0.1")
    assert manifest["contract_version"] == manifest["release_id"] == "v0.1"
    assert manifest["schema_sha256"] == digest(schema_description(base_schema()))
    assert manifest["storage_format"] == "lance"
    assert manifest["table_path"] == "samples.lance"
    assert manifest["rejected_rows"] == 0
    assert manifest["excluded_source_records"] == []
    assert manifest["checkpoint_code_versions"] == {}
    assert manifest["finalization"] is None
    assert manifest["code_migration"] is None
    assert release_dataset(tmp_path / "v0.1").version == manifest["lance_version"]


def test_reader_rejects_implicit_latest_and_unpublished_directory(tmp_path):
    import pytest

    for name, version in [("v0.1", None), ("v0.1.incomplete", 2)]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "storage_format": "lance",
                    "table_path": "samples.lance",
                    "lance_version": version,
                }
            )
        )
        with pytest.raises(ValueError, match="explicit positive snapshot"):
            release_dataset(folder)
