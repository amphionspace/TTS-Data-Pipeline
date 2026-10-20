"""Effective bandwidth of the float32 channel mean: where the long-term spectrum stops.

Lossy upstream codecs (low-bitrate MP3/AAC) low-pass the audio; the band above the encoder
cut-off is then empty in every model input (mel, codec). This measures where the spectrum
ends relative to the speech band, and how abruptly; it does not judge quality.
"""

import hashlib
import math

import numpy as np
import soundfile as sf
from scipy.signal import welch

from ...schema import digest
from ..audio_stats.metrics import InvalidAudio, decode

EDGE_LEVELS_DB = (40, 50, 60)
BANDS_HZ = {"4_8k": (4000, 8000), "8_12k": (8000, 12000), "12_16k": (12000, 16000)}
REFERENCE_HZ = (100, 4000)
DROP_HZ = (250, 750)
MIN_SEGMENTS = 8
SILENT_REFERENCE_DB = -150.0

DEFINITION = {
    "name": "welch-band-edge-v1",
    "decode": "soundfile.read_whole_file_float32_always_2d",
    "channels": "float32_arithmetic_mean_over_channels",
    "resampling": "none",
    "segment_samples": "2 ** round(log2(native_sample_rate / 24)) (21-31 Hz bins)",
    "spectrum": "scipy.signal.welch(window='hann', noverlap=segment//2, detrend='constant', "
    "scaling='density', average='mean') in float64; level_db = 10*log10(psd + 1e-20)",
    "reference": "numpy.percentile(level_db over 100 <= f < 4000 Hz, 95, method='lower')",
    "relative_db": "level_db - reference",
    "edge_hz_N": "highest bin frequency with relative_db > -N (N = 40, 50, 60); at least the "
    "reference bin, so never below 100 Hz",
    "band_db_X": "mean relative_db over bins with low <= f < high, null if the band starts at or "
    "above Nyquist; 16k_up covers 16 kHz to Nyquist",
    "edge_drop_db": "relative_db at the edge_hz_50 bin minus mean relative_db over bins with "
    "edge+250 <= f < edge+750 Hz; null if that range reaches Nyquist",
    "encoded_kbps": "len(encoded bytes) * 8 / decoded duration / 1000 "
    "(container overhead included)",
    "container": "magic bytes: ID3 or MPEG frame sync -> mp3, fLaC -> flac, RIFF -> wav, "
    "OggS -> ogg, ftyp at offset 4 -> mp4, else unknown",
    "minimum": "at least 8 segments of samples, otherwise audio_too_short",
    "silence": "all-zero mono mix, or reference below -150 dB (density floor is -200) -> "
    "audio_silent",
    "interpretation": "measurements_only_not_a_quality_verdict",
}


def container(encoded):
    if encoded[:3] == b"ID3" or (encoded[:1] == b"\xff" and encoded[1:2] >= b"\xe0"):
        return "mp3"
    if encoded[:4] == b"fLaC":
        return "flac"
    if encoded[:4] == b"RIFF":
        return "wav"
    if encoded[:4] == b"OggS":
        return "ogg"
    if encoded[4:8] == b"ftyp":
        return "mp4"
    return "unknown"


def segment_samples(rate):
    return 2 ** round(math.log2(rate / 24))


def measure(wave, rate):
    frames = len(wave)
    segment = segment_samples(rate)
    if frames < MIN_SEGMENTS * segment:
        raise InvalidAudio("audio_too_short")
    if not np.isfinite(wave).all():
        raise InvalidAudio("audio_nonfinite")
    mono = wave.mean(axis=1, dtype=np.float32)
    if not mono.any():
        raise InvalidAudio("audio_silent")
    freqs, psd = welch(
        mono.astype(np.float64),
        rate,
        window="hann",
        nperseg=segment,
        noverlap=segment // 2,
        detrend="constant",
        scaling="density",
        average="mean",
    )
    level = 10 * np.log10(psd + 1e-20)
    speech = (freqs >= REFERENCE_HZ[0]) & (freqs < REFERENCE_HZ[1])
    reference = float(np.percentile(level[speech], 95, method="lower"))
    if reference < SILENT_REFERENCE_DB:
        # Clicks or dither in silence: every bin sits near the floor and would read as full band.
        raise InvalidAudio("audio_silent")
    relative = level - reference
    nyquist = rate / 2
    result = dict(
        native_sample_rate=rate,
        duration_seconds=frames / rate,
        segment_samples=segment,
        nyquist_hz=nyquist,
        reference_level_db=reference,
    )
    edges = {}
    for n in EDGE_LEVELS_DB:
        edges[n] = np.flatnonzero(relative > -n)[-1]  # the reference bin itself always qualifies
        result[f"edge_hz_{n}"] = float(freqs[edges[n]])
    for name, (low, high) in [*BANDS_HZ.items(), ("16k_up", (16000, math.inf))]:
        band = (freqs >= low) & (freqs < high)
        result[f"band_db_{name}"] = float(relative[band].mean()) if low < nyquist else None
    edge = result["edge_hz_50"]
    above = (freqs >= edge + DROP_HZ[0]) & (freqs < edge + DROP_HZ[1])
    result["edge_drop_db"] = (
        float(relative[edges[50]] - relative[above].mean())
        if edge + DROP_HZ[1] <= nyquist
        else None
    )
    return result


def process(row, profile_id):
    """One published row per sample; failures keep identity and a null result."""
    encoded = row["audio"]["bytes"]
    if encoded is None or hashlib.sha256(encoded).hexdigest() != row["audio_sha256"]:
        raise RuntimeError("Source audio hash conflict")
    fingerprint = digest(
        [
            "effective-bandwidth-input-v1",
            row["sample_id"],
            row["audio_sha256"],
            "full_actual_native_decode",
            profile_id,
        ]
    )
    output = dict(
        sample_id=row["sample_id"],
        input_fingerprint=fingerprint,
        status="ok",
        error_code=None,
        result=None,
    )
    try:
        wave, rate, warnings = decode(encoded)
        if len(wave) and (rate != row["sample_rate"] or wave.shape[1] != row["channels"]):
            raise InvalidAudio("native_format_mismatch")
        result = measure(wave, rate)
    except sf.LibsndfileError:
        return dict(output, status="failed", error_code="audio_decode_failed")
    except InvalidAudio as exc:
        return dict(output, status="failed", error_code=str(exc))
    result.update(
        audio_sha256=row["audio_sha256"],
        num_samples_delta=round(result["duration_seconds"] * rate) - row["num_frames"],
        container=container(encoded),
        encoded_kbps=len(encoded) * 8 / result["duration_seconds"] / 1000,
        decoder_warnings=warnings,
    )
    return dict(output, result=result)
