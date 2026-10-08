import hashlib
import io

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline.codec.qwen3_12hz.audio import array_sha256, decode, validate_codes, waveform
from tts_data_pipeline.codec.qwen3_12hz.profile import feature_row


def audio_row():
    audio = np.sin(np.arange(22051, dtype=np.float32) / 17)
    buffer = io.BytesIO()
    sf.write(buffer, np.stack([audio, -audio], axis=1), 22050, format="WAV", subtype="FLOAT")
    data = buffer.getvalue()
    return dict(
        audio={"bytes": data},
        audio_sha256=hashlib.sha256(data).hexdigest(),
        num_frames=22051,
        sample_rate=22050,
        channels=2,
    )


def test_full_sample_not_source_timestamp_crop():
    row = audio_row()
    row["metadata_json"] = '{"begin_time":12,"end_time":13}'
    wave = waveform(row)
    assert wave.dtype == np.float32 and wave.shape == (24002,)
    assert np.array_equal(wave, np.zeros(24002, dtype=np.float32))


@pytest.mark.parametrize(
    "key,value",
    [
        ("sample_rate", 24000),
        ("channels", 1),
        ("audio_sha256", "0" * 64),
        ("segment_start_frame", 0),
    ],
)
def test_waveform_rejects_changed_identity_and_implicit_view(key, value):
    row = audio_row()
    row[key] = value
    with pytest.raises(ValueError):
        waveform(row)


def test_codes_reject_invalid_values_before_narrowing():
    codes = np.zeros((13, 16), dtype=np.int64)
    assert validate_codes(codes, 24000).dtype == np.int16
    for invalid in [
        codes.astype(float),
        codes - 1,
        codes + 65536,
        codes + 2048,
        codes[:12],
        codes[:, :15],
    ]:
        with pytest.raises(ValueError):
            validate_codes(invalid, 24000)


def test_array_digest_canonicalizes_byte_order_and_memory_layout():
    codes = np.arange(32, dtype=np.int16).reshape(2, 16)
    expected = array_sha256(codes, ["time", "codebook"])
    assert array_sha256(codes.astype(">i2"), ["time", "codebook"]) == expected
    assert array_sha256(np.asfortranarray(codes), ["time", "codebook"]) == expected
    assert array_sha256(codes, ["codebook", "time"]) != expected


@pytest.mark.parametrize("hint", [22050, 22052, 1, 100000])
def test_actual_decode_controls_waveform_and_identity(hint):
    row = {**audio_row(), "sample_id": "a" * 64}
    definition = {"timeline_profile_id": "b" * 64}
    reference, frames = decode(row)
    codes = np.zeros(((len(reference) + 1919) // 1920, 16), dtype=np.int16)
    expected = feature_row(row, definition, reference, codes, native_frames=frames)
    row["num_frames"] = hint
    wave, frames = decode(row)
    assert frames == 22051
    assert np.array_equal(wave, reference)
    assert feature_row(row, definition, wave, codes, native_frames=frames) == expected
    assert row["num_frames"] == hint
