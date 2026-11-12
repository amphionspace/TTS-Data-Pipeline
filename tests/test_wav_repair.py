import hashlib
import io
import struct

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline import wav_repair
from tts_data_pipeline.dots_vae import model, profile


def caf_wav(pcm, rate=16000, flags=2, bits=16):
    """WAV whose data chunk is a whole CAF file (the FlexiVoice kespeech layout)."""
    desc = struct.pack(">dIIIIII", float(rate), 0x6C70636D, flags, 2, 1, 1, bits)
    caf = b"caff" + struct.pack(">HH", 1, 0)
    caf += b"desc" + struct.pack(">q", len(desc)) + desc
    caf += b"free" + struct.pack(">q", 4) + b"\0" * 4
    caf += b"data" + struct.pack(">q", -1) + struct.pack(">I", 0) + pcm
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(caf)) + caf
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_header_size_matches_the_bytes_before_pcm():
    pcm = (np.sin(np.arange(1600) / 5) * 8000).astype("<i2").tobytes()
    raw = caf_wav(pcm)
    header = wav_repair.embedded_caf_header(raw, 16000, 1)
    assert raw[wav_repair.wav_data_offset(raw) + header :] == pcm


def test_plain_wav_has_no_header():
    buf = io.BytesIO()
    sf.write(buf, np.full(160, 0.1, np.float32), 16000, subtype="PCM_16", format="WAV")
    assert wav_repair.embedded_caf_header(buf.getvalue(), 16000, 1) == 0


@pytest.mark.parametrize("kwargs", [dict(rate=8000), dict(flags=0), dict(flags=3), dict(bits=24)])
def test_header_describing_another_stream_is_rejected(kwargs):
    raw = caf_wav(b"\0\0" * 8, **{k: v for k, v in kwargs.items()})
    with pytest.raises(ValueError, match="differs"):
        wav_repair.embedded_caf_header(raw, 16000, 1)


def encoder(policy):
    e = model.Encoder.__new__(model.Encoder)
    e.up, e.waveform_policy = model.upstream(), policy
    return e


def row_of(raw):
    info = sf.info(io.BytesIO(raw))
    return dict(audio={"bytes": raw}, audio_sha256=hashlib.sha256(raw).hexdigest(),
                sample_rate=info.samplerate, channels=info.channels, num_frames=info.frames)


def test_raw_policy_skips_the_header_and_keeps_the_edges():
    speech = (np.sin(np.arange(16000) / 7) * 12000).astype("<i2")  # loud from the first sample
    raw = caf_wav(speech.tobytes())
    padded, info, error = encoder("raw-caf-repair-v1").waveform(row_of(raw))
    assert error is None
    header_frames = wav_repair.embedded_caf_header(raw, 16000, 1) // 2
    assert info["start_frame"] == header_frames and info["end_frame"] == header_frames + 16000
    assert info["edge_leading_delta"] == info["edge_trailing_delta"] == 0
    assert info["processed_frames"] == info["resampled_frames"] == 48000
    assert padded.size(-1) % model.ALIGN == 0
    # No leading zeros were inserted: the waveform starts with the speech itself.
    assert float(padded[0, :480].abs().max()) > 0.05


def test_default_policy_is_the_dots_chain_and_leaves_the_header():
    raw = caf_wav((np.ones(16000) * 12000).astype("<i2").tobytes())
    _, info, error = encoder(model.WAVEFORM_POLICY).waveform(row_of(raw))
    assert error is None and info["start_frame"] == 0 and info["edge_leading_delta"] is not None


def test_default_waveform_section_is_unchanged():
    section = profile.waveform_section(model.WAVEFORM_POLICY)
    assert "policy" not in section and "embedded_caf" not in section
    assert section["order"][3] == "normalize_edge_silence"
    assert profile.waveform_section("raw-caf-repair-v1")["order"][0] == "skip_embedded_caf_header"
    assert profile.profile_name("dots-reference-v2:min-3s-on-patches") == "dots-tts-soar-vae-xvec-48k-fp32-v2-ref3s"
    assert profile.profile_name("dots-reference-v2:min-3s-on-patches", "raw-caf-repair-v1") == "dots-tts-soar-vae-xvec-48k-fp32-v3-ref3s"


def test_feature_row_with_a_skipped_header_validates():
    info = dict(start_frame=2048, end_frame=18048)
    row = dict(sample_id="t", audio_sha256="a" * 64, sample_rate=16000, num_frames=18048,
               text="x", text_kind="k", language="zh")
    definition = dict(timeline_profile_id="b" * 64)
    stored = profile.feature_row(row, definition, info, "silent_audio", "c" * 64)
    assert stored["start_frame"] == 2048
    profile.validate_row(stored)  # identity and feature_key agree on the start frame
