"""Whole-file waveform statistics on the float32 channel mean consumed by feature models."""

import hashlib
import io
import os
import tempfile

import numpy as np
import soundfile as sf

from ...schema import digest

LOW_ENERGY_MEAN_SQUARE = 10 ** (-50 / 10)
NEAR_FULL_SCALE = 0.999
CHUNK_WINDOWS = 4096

DEFINITION = {
    "name": "mono-mix-waveform-stats-v1",
    "decode": "soundfile.read_whole_file_float32_always_2d",
    "channels": "float32_arithmetic_mean_over_channels",
    "accumulation": "float64",
    "resampling": "none",
    "padding": "none",
    "intervals": "native_sample_half_open",
    "window_samples": "max(1, native_sample_rate // 50)",
    "last_window": "use_actual_length_no_padding",
    "low_energy": "window_rms <= 10**(-50/20)",
    "near_full_scale": "abs(value) >= 0.999",
    "window_rms_percentiles": "numpy.percentile(window_rms, [10, 90], method='lower')",
    "zero_run": "exact 0.0 in mono mix; internal runs only",
    "decoder_warnings": "lines written to stderr fd 2 during decode",
    "interpretation": "measurements_only_not_speech_detection_or_quality_verdict",
}


class InvalidAudio(ValueError):
    pass


_STDERR_SINK = None


def decode(encoded):
    """Decode the whole file, counting decoder lines written to file descriptor 2."""
    global _STDERR_SINK
    if _STDERR_SINK is None:
        _STDERR_SINK = tempfile.TemporaryFile()
    sink = _STDERR_SINK.fileno()
    start = os.lseek(sink, 0, os.SEEK_END)
    saved = os.dup(2)
    os.dup2(sink, 2)
    try:
        wave, rate = sf.read(io.BytesIO(encoded), dtype="float32", always_2d=True)
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        end = os.lseek(sink, 0, os.SEEK_END)
        written = os.pread(sink, end - start, start) if end > start else b""
        if end > 1 << 26:
            os.ftruncate(sink, 0)
    return wave, rate, written.count(b"\n")


def runs(mask):
    """Half-open [start, end) runs of True values."""
    edges = np.flatnonzero(np.diff(np.r_[0, mask.view(np.int8), 0]))
    return edges[0::2], edges[1::2]


def measure(wave, rate):
    frames, channels = wave.shape
    if not frames:
        raise InvalidAudio("audio_empty")
    if not np.isfinite(wave).all():
        raise InvalidAudio("audio_nonfinite")
    mono = wave.mean(axis=1, dtype=np.float32)
    window = max(1, rate // 50)
    chunk = window * CHUNK_WINDOWS
    peak = squares = total = 0.0
    near = 0
    energies = []
    for offset in range(0, frames, chunk):
        values = mono[offset : offset + chunk].astype(np.float64)
        absolute = np.abs(values)
        peak = max(peak, float(absolute.max()))
        squared = values * values
        squares += float(squared.sum())
        total += float(values.sum())
        near += int(np.count_nonzero(absolute >= NEAR_FULL_SCALE))
        starts = np.arange(0, len(values), window)
        lengths = np.minimum(window, len(values) - starts)
        energies.append(np.add.reduceat(squared, starts) / lengths)
    energy = np.concatenate(energies)
    low_start, low_end = runs(energy <= LOW_ENERGY_MEAN_SQUARE)
    low_start, low_end = low_start * window, np.minimum(low_end * window, frames)
    low_lengths = low_end - low_start
    internal = (low_start > 0) & (low_end < frames)
    zero_start, zero_end = runs(mono == 0)
    zero_internal = (zero_start > 0) & (zero_end < frames)
    p10, p90 = np.percentile(np.sqrt(energy), [10, 90], method="lower")
    return dict(
        native_sample_rate=rate,
        source_channels=channels,
        decoded_num_samples=frames,
        duration_seconds=frames / rate,
        peak=peak,
        rms=float(np.sqrt(squares / frames)),
        dc_offset=total / frames,
        near_full_scale_ratio=near / frames,
        low_energy_ratio=int(low_lengths.sum()) / frames,
        leading_low_energy_samples=int(low_end[0]) if len(low_start) and low_start[0] == 0 else 0,
        trailing_low_energy_samples=int(frames - low_start[-1])
        if len(low_end) and low_end[-1] == frames
        else 0,
        longest_low_energy_samples=int(low_lengths.max(initial=0)),
        longest_internal_low_energy_samples=int(low_lengths[internal].max(initial=0)),
        window_rms_p10=float(p10),
        window_rms_p90=float(p90),
        longest_internal_zero_run_samples=int(
            (zero_end - zero_start)[zero_internal].max(initial=0)
        ),
    )


def process(row, profile_id):
    """One published row per sample; failures keep identity and a null result."""
    encoded = row["audio"]["bytes"]
    if encoded is None or hashlib.sha256(encoded).hexdigest() != row["audio_sha256"]:
        raise RuntimeError("Source audio hash conflict")
    fingerprint = digest(
        [
            "audio-stats-input-v1",
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
        num_samples_delta=result["decoded_num_samples"] - row["num_frames"],
        decoder_warnings=warnings,
    )
    return dict(output, result=result)
