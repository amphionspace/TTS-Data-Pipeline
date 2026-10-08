"""Codec input decoding, waveform validation, and canonical array hashes.

Only whole samples are supported here; source recording timestamps are not
coordinates on already-cut audio bytes. This module does not import GPU code.
"""

import hashlib
import io
import math

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from ...feature_audio import array_sha256 as array_sha256
from ...feature_audio import canonical as canonical


def waveform(row):
    return decode(row)[0]


def decode(row):
    """Decode the entire base sample; source recording timestamps are not crop coordinates."""
    data = row["audio"]["bytes"]
    if not data or hashlib.sha256(data).hexdigest() != row["audio_sha256"]:
        raise ValueError("audio_sha256_mismatch")
    audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    if sr != row["sample_rate"] or audio.shape[1] != row["channels"]:
        raise ValueError(
            f"native_format_mismatch: sample={row.get('sample_id')} "
            f"expected_rate_channels={(row['sample_rate'], row['channels'])} "
            f"actual_rate_channels={(sr, audio.shape[1])}"
        )
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError("invalid_native_waveform")
    if any(
        row.get(k) is not None
        for k in ("parent_sample_id", "segment_start_frame", "segment_end_frame")
    ):
        raise ValueError("sample_requires_explicit_view")
    mono = audio.mean(axis=1, dtype=np.float32)
    if sr != 24000:
        divisor = math.gcd(sr, 24000)
        mono = resample_poly(
            mono,
            24000 // divisor,
            sr // divisor,
            window=("kaiser", 5.0),
            padtype="constant",
            cval=0.0,
        )
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    if len(mono) != (len(audio) * 24000 + sr - 1) // sr or not np.isfinite(mono).all():
        raise ValueError("invalid_resampled_waveform")
    # Container frame counts are scheduling hints, never pad/trim coordinates.
    return mono, len(audio)


def validate_codes(codes, num_input_frames):
    codes = np.asarray(codes)
    if codes.dtype.kind not in "iu":
        raise ValueError("Encoder must produce integers before storage casting")
    expected = (num_input_frames + 1919) // 1920
    if codes.shape != (expected, 16) or expected <= 0:
        raise ValueError(f"Unexpected code shape {codes.shape}; expected {(expected, 16)}")
    if np.any(codes < 0) or np.any(codes >= 2048):
        raise ValueError("Codec value outside [0,2048)")
    return np.ascontiguousarray(codes, dtype=np.int16)
