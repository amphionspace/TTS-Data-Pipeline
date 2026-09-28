import hashlib
import io

import lance
import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline.schema import (
    make_record,
    record_revision,
    sample_id,
    validate_record,
)
from tts_data_pipeline.writer import write_records


def example():
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(1600), 16000, format="WAV", subtype="PCM_16")
    return make_record(
        dataset_id="test",
        source_snapshot="v1",
        source_key="utt-1",
        audio_bytes=buffer.getvalue(),
        audio_name="example.wav",
        source_locator={"archive": "source.tar", "member": "example.wav"},
    )


def test_lance_roundtrip_preserves_bytes_and_unknowns(tmp_path):
    row = example()
    target = tmp_path / "base.lance"
    write_records([row], target)
    dataset = lance.dataset(target)
    restored = dataset.to_table().to_pylist()[0]
    validate_record(restored)
    assert restored["text"] is None and restored["speaker_id"] is None
    assert restored["audio"]["bytes"] == row["audio"]["bytes"]
    assert restored["text_variants"] == []
    validate_record(next(dataset.to_batches()).to_pylist()[0])


def test_text_correction_preserves_identity_and_codec_audio_input():
    row = example()
    old_id, old_revision, audio_hash = row["sample_id"], row["record_revision"], row["audio_sha256"]
    row.update(text="你好", text_kind="human_transcript")
    row["record_revision"] = record_revision(row)
    validate_record(row)
    assert row["sample_id"] == old_id and row["record_revision"] != old_revision
    assert row["audio_sha256"] == audio_hash
    assert sample_id("test", "v1", "utt-1") != sample_id("test", "v1", "utt-2")


def test_stale_audio_and_blank_unknown_rejected():
    row = example()
    row["audio_sha256"] = hashlib.sha256(b"changed").hexdigest()
    with pytest.raises(ValueError, match="Audio hash mismatch"):
        validate_record(row)
    row = example()
    row["speaker_id"] = ""
    with pytest.raises(ValueError, match="Use null"):
        validate_record(row)


def test_contract_enforces_split_variants_and_finite_json():
    for key, value in [
        ("source_split", "test"),
        ("text_variants", [{"kind": "asr", "text": "", "language": None}]),
        ("metadata_json", '{"score":NaN}'),
    ]:
        row = example()
        row[key] = value
        row["record_revision"] = record_revision(row)
        with pytest.raises(ValueError):
            validate_record(row)


def test_added_annotation_does_not_change_base_revision():
    row = example()
    expected = record_revision(row)
    row["ann__quality__run_001"] = {"status": "ok", "result": {"score": 4.0}}
    assert record_revision(row) == expected
