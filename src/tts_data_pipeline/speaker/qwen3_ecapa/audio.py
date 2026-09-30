"""Training-compatible whole-sample waveform and mel frontend, without batch padding."""

import hashlib
import io
import math

import numpy as np
import soundfile as sf
import torch
import torchaudio.functional as AF
from qwen_tts.core.models.modeling_qwen3_tts import mel_spectrogram

from ...feature_audio import array_sha256

MEL_OPTIONS = dict(
    n_fft=1024,
    num_mels=128,
    sampling_rate=24000,
    hop_size=256,
    win_size=1024,
    fmin=0,
    fmax=12000,
    center=False,
)


class InvalidAudio(ValueError):
    """An individual input cannot be represented by the native encoder."""


def resampled_frames(frames, rate):
    """Match torchaudio's FP32 ceil, including rounding near integer boundaries.

    This predicts the native output; it never pads or trims the waveform.
    """
    if rate == 24000:
        return frames
    divisor = math.gcd(rate, 24000)
    return math.ceil(float(np.float32((24000 // divisor) * frames / (rate // divisor))))


def frontend(row):
    raw = row["audio"]["bytes"]
    if not raw or hashlib.sha256(raw).hexdigest() != row["audio_sha256"]:
        raise ValueError("audio_sha256_mismatch")
    if any(
        row.get(k) is not None
        for k in ("parent_sample_id", "segment_start_frame", "segment_end_frame")
    ):
        raise ValueError("sample_requires_explicit_view")
    try:
        audio, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except sf.LibsndfileError as exc:
        raise InvalidAudio("audio_decode_failed") from exc
    # Container/base frame counts are scheduling hints. Keep the complete decode.
    if rate != row["sample_rate"] or audio.shape[1] != row["channels"]:
        raise InvalidAudio("native_format_mismatch")
    if not len(audio) or not np.isfinite(audio).all():
        raise InvalidAudio("invalid_native_waveform")
    wave = torch.from_numpy(np.ascontiguousarray(audio.mean(axis=1)))
    if rate != 24000:
        wave = AF.resample(wave, rate, 24000)
    frames = len(wave)
    if frames != resampled_frames(len(audio), rate):
        raise ValueError("resampled_length_mismatch")
    if not torch.isfinite(wave).all():
        raise InvalidAudio("invalid_resampled_waveform")
    info = dict(
        end_frame=len(audio),
        encoder_input_num_frames=frames,
        encoder_input_sha256=array_sha256(wave.numpy(), ["sample"]),
    )
    # Native ECAPA reflection with dilation=4 requires at least 5 mel frames.
    if frames < 1280:
        return None, info, "speaker_input_too_short"
    mel = mel_spectrogram(wave[None], **MEL_OPTIONS)[0].T.contiguous()
    if not torch.isfinite(mel).all():
        return None, info, "invalid_mel"
    return mel, info, None


def decode(row):
    try:
        return frontend(row)
    except InvalidAudio as exc:
        return None, dict(encoder_input_num_frames=None, encoder_input_sha256=None), str(exc)
