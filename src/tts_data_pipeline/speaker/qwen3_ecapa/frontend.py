"""Complete actual decoded waveform and native-equivalent cached FP32 resampling."""

import hashlib
from functools import lru_cache

import numpy as np
import soundfile as sf
import torch
from torchaudio.transforms import Resample

from ...feature_audio import array_sha256
from .audio import InvalidAudio, resampled_frames
from .decoder import read_audio, read_native_audio


@lru_cache(maxsize=32)
def resampler(rate):
    # Explicit dtype reproduces functional.resample's FP32 kernel construction.
    return Resample(
        rate,
        24000,
        lowpass_filter_width=6,
        rolloff=0.99,
        resampling_method="sinc_interp_hann",
        beta=None,
        dtype=torch.float32,
    )


def waveform(row):
    reference = row.get("reference")
    if reference and reference.get("error_code"):
        return None, reference_info(reference), reference["error_code"]
    raw = row["audio"]["bytes"]
    if not raw or hashlib.sha256(raw).hexdigest() != row["audio_sha256"]:
        raise ValueError("audio_sha256_mismatch")
    if any(
        row.get(k) is not None
        for k in ["parent_sample_id", "segment_start_frame", "segment_end_frame"]
    ):
        raise ValueError("sample_requires_explicit_view")
    try:
        if reference:
            # Match the pinned codec native timeline, including Opus decoding.
            audio, rate = read_native_audio(raw)
        else:
            audio, rate = read_audio(raw)
    except sf.LibsndfileError as exc:
        raise InvalidAudio("audio_decode_failed") from exc
    if rate != row["sample_rate"] or audio.shape[1] != row["channels"]:
        raise InvalidAudio("native_format_mismatch")
    if not len(audio) or not np.isfinite(audio).all():
        raise InvalidAudio("invalid_native_waveform")
    if reference:
        if len(audio) != reference["reference_native_total_frames"]:
            raise InvalidAudio("reference_native_length_mismatch")
        audio = audio[reference["start_frame"] : reference["end_frame"]]
    wave = torch.from_numpy(np.ascontiguousarray(audio.mean(axis=1)))
    if rate != 24000:
        wave = resampler(rate)(wave)
    if len(wave) != resampled_frames(len(audio), rate):
        raise ValueError("resampled_length_mismatch")
    if not torch.isfinite(wave).all():
        raise InvalidAudio("invalid_resampled_waveform")
    info = dict(
        end_frame=len(audio),
        encoder_input_num_frames=len(wave),
        encoder_input_sha256=array_sha256(wave.numpy(), ["sample"]),
    )
    if reference:
        info.update(reference_info(reference))
    if len(wave) < 1280:
        return None, info, "speaker_input_too_short"
    return wave, info, None


def reference_info(reference):
    return {k: v for k, v in reference.items() if k not in {"error_code", "target_id"}}


def decode_waveform(row):
    try:
        return waveform(row)
    except InvalidAudio as exc:
        info = dict(encoder_input_num_frames=None, encoder_input_sha256=None)
        if row.get("reference"):
            info.update(reference_info(row["reference"]))
        return None, info, str(exc)
