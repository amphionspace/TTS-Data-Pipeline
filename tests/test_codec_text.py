from pathlib import Path

import lance
import pyarrow as pa
import pytest
from test_codec_run import TEXT_SOURCES, fixture

from tts_data_pipeline.codec.qwen3_12hz.text import materialize_text, selected_metadata
from tts_data_pipeline.schema import digest


def test_selected_text_and_language_keep_raw_revision_identity():
    row = dict(
        text=" original ",
        selected_text="original",
        selected_text_source=0,
        language="en-US",
        selected_language="en",
        dataset_id="test",
        source_key="1",
        speaker_id=None,
        speaker_scope=None,
        text_kind="transcript",
    )
    result = selected_metadata(row, TEXT_SOURCES)
    assert result["text"] == "original" and result["language"] == "en"
    assert result["text_revision"] == digest(
        ["selected-text-v1", " original ", TEXT_SOURCES["0"]["normalization"]]
    )
    for changes in [
        dict(selected_text=""),
        dict(selected_text=None),
        dict(selected_text_source=1),
        dict(selected_text_source=None),
    ]:
        with pytest.raises(ValueError):
            selected_metadata({**row, **changes}, TEXT_SOURCES)
    with pytest.raises(ValueError, match="pinned raw revisions"):
        selected_metadata(
            {**row, "selected_text_source": 1},
            {**TEXT_SOURCES, "1": {"kind": "annotation"}},
        )
    fallback = selected_metadata(
        {**row, "selected_text": None, "selected_text_source": None, "selected_language": None},
        TEXT_SOURCES,
    )
    assert fallback["text"] == " original " and fallback["language"] == "en-US"


def test_text_materialization_rejects_misaligned_targets(tmp_path):
    rows, p, d = fixture(tmp_path)
    ids = [row["sample_id"] for row in rows[::2]]
    source = lance.dataset(Path(p["root"]) / d["source"]["table_path"]).checkout_version(
        (d["source"]["branch"], d["source"]["lance_version"])
    )
    features = lance.write_dataset(pa.table({"target_id": ids[::-1]}), tmp_path / "wrong.lance")
    with pytest.raises(Exception, match="Codec/text target order differs"):
        materialize_text(features, source, d["tasks"], TEXT_SOURCES)
    features = lance.write_dataset(pa.table({"target_id": ids}), tmp_path / "good.lance")
    result = materialize_text(features, source, d["tasks"], TEXT_SOURCES)
    assert result["rows"] == 5 and result["original_codec_files_preserved"]
    with pytest.raises(ValueError, match="fresh unpublished"):
        materialize_text(features, source, d["tasks"], TEXT_SOURCES)
