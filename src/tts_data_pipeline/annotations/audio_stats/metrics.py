"""Streaming waveform statistics without resampling, mixing, or padding."""

import hashlib
import io

import numpy as np
import soundfile as sf

from ...schema import digest

DEFINITION = {
    "name": "native-waveform-stats-v1",
    "decode": "libsndfile_float32_blocks",
    "accumulation": "float64",
    "channels": "preserve_all_channels_no_mixdown",
    "resampling": "none",
    "padding": "none",
    "intervals": "native_sample_half_open",
    "window_samples": "max(1, native_sample_rate // 50)",
    "last_window": "use_actual_length_no_padding",
    "low_energy": "every_channel_window_rms <= 10**(-50/20)",
    "near_full_scale": "abs(value) >= 0.999",
    "at_or_above_full_scale": "abs(value) >= 1.0",
    "aggregate_rms": "sqrt(sum_all_channels_squared / (frames * channels))",
    "interpretation": "measurements_only_not_speech_detection_or_quality_verdict",
}


class InvalidAudio(ValueError):
    pass


def measure(encoded):
    with sf.SoundFile(io.BytesIO(encoded)) as source:
        rate, channels = source.samplerate, source.channels
        window = max(1, rate // 50)
        peak = np.zeros(channels, dtype=np.float64)
        squares = np.zeros(channels, dtype=np.float64)
        sums = np.zeros(channels, dtype=np.float64)
        near = np.zeros(channels, dtype=np.int64)
        full = np.zeros(channels, dtype=np.int64)
        zeros = np.zeros(channels, dtype=np.int64)
        intervals = []
        frames = 0
        for block in source.blocks(blocksize=window * 256, dtype="float32", always_2d=True):
            if not np.isfinite(block).all():
                raise InvalidAudio("audio_nonfinite")
            values = block.astype(np.float64)
            absolute = np.abs(values)
            peak = np.maximum(peak, absolute.max(axis=0))
            squared = values * values
            squares += squared.sum(axis=0)
            sums += values.sum(axis=0)
            near += (absolute >= 0.999).sum(axis=0)
            full += (absolute >= 1.0).sum(axis=0)
            zeros += (values == 0).sum(axis=0)
            starts = np.arange(0, len(values), window)
            lengths = np.minimum(window, len(values) - starts)
            energies = np.add.reduceat(squared, starts, axis=0) / lengths[:, None]
            low = np.all(energies <= 10 ** (-50 / 10), axis=1)
            edges = np.diff(np.r_[False, low, False].astype(np.int8))
            for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
                start, end = frames + int(a) * window, frames + min(int(b) * window, len(values))
                if intervals and intervals[-1]["end_sample"] == start:
                    intervals[-1]["end_sample"] = end
                else:
                    intervals.append(dict(start_sample=start, end_sample=end))
            frames += len(values)
        if not frames:
            raise InvalidAudio("audio_empty")
        low_samples = sum(i["end_sample"] - i["start_sample"] for i in intervals)
        return dict(
            native_sample_rate=rate,
            channels=channels,
            decoded_num_samples=frames,
            duration_seconds=frames / rate,
            start_sample=0,
            end_sample=frames,
            peak=float(peak.max()),
            rms=float(np.sqrt(squares.sum() / (frames * channels))),
            near_full_scale_ratio=float(near.sum() / (frames * channels)),
            at_or_above_full_scale_ratio=float(full.sum() / (frames * channels)),
            zero_ratio=float(zeros.sum() / (frames * channels)),
            channel_stats=[
                dict(
                    channel=i,
                    peak=float(peak[i]),
                    rms=float(np.sqrt(squares[i] / frames)),
                    dc_offset=float(sums[i] / frames),
                    near_full_scale_ratio=float(near[i] / frames),
                    at_or_above_full_scale_ratio=float(full[i] / frames),
                    zero_ratio=float(zeros[i] / frames),
                )
                for i in range(channels)
            ],
            low_energy_intervals=intervals,
            low_energy_ratio=low_samples / frames,
            leading_low_energy_samples=intervals[0]["end_sample"]
            if intervals and intervals[0]["start_sample"] == 0
            else 0,
            trailing_low_energy_samples=frames - intervals[-1]["start_sample"]
            if intervals and intervals[-1]["end_sample"] == frames
            else 0,
            longest_low_energy_samples=max(
                (i["end_sample"] - i["start_sample"] for i in intervals), default=0
            ),
        )


def process(row, profile_id):
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
    target = dict(
        target_kind="sample",
        target_id=row["sample_id"],
        input_fingerprint=fingerprint,
        status="ok",
        error_code=None,
        item_count=1,
    )
    try:
        result = measure(encoded)
    except (sf.LibsndfileError, InvalidAudio) as exc:
        code = str(exc) if isinstance(exc, InvalidAudio) else "audio_decode_failed"
        return dict(target, status="failed", error_code=code, item_count=0), None
    result.update(
        target_kind="sample",
        target_id=row["sample_id"],
        item_id=0,
        input_fingerprint=fingerprint,
        audio_sha256=row["audio_sha256"],
        declared_sample_rate=row["sample_rate"],
        declared_channels=row["channels"],
        declared_num_samples=row["num_frames"],
        num_samples_delta=result["decoded_num_samples"] - row["num_frames"],
        header_format_matches=(
            result["native_sample_rate"] == row["sample_rate"]
            and result["channels"] == row["channels"]
        ),
    )
    return target, result
