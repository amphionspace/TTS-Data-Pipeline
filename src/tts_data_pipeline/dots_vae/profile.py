"""Pinned dots.tts weights, upstream source and preprocessing; row identity and validation."""

import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

from ..feature_audio import array_sha256
from ..feature_runtime import file_hash
from ..schema import digest
from . import model

PROFILE_NAME = "dots-tts-soar-vae-xvec-48k-fp32-v1"
KIND = "dots_vae"
LATENT_DIM = 128
XVECTOR_DIM = 512


def profile(model_root):
    root = Path(model_root)
    source = model.DOTS_SOURCE
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ).strip()
    if revision != model.DOTS_REVISION or dirty:
        raise ValueError("dots.tts source is not the pinned clean revision")
    config = json.loads((root / "config.json").read_text())
    timeline = dict(
        decoder="soundfile",
        soundfile_version=sf.__version__,
        libsndfile_version=sf.__libsndfile_version__,
        output_rate="native",
        output_channels="native",
        dtype="float32",
        length_policy="use_complete_actual_decode",
        implicit_pad_or_trim="none",
    )
    return {
        "kind": KIND,
        "architecture": "dots.tts AudioVAE encoder posterior + CAM++ x-vector",
        "model": dict(
            name="dots.tts-soar",
            files={name: file_hash(root / name) for name in model.MODEL_FILES},
            vocoder_config=config["vocoder"],
            campplus_embedding_size=config["campplus_embedding_size"],
            xvec_max_audio_seconds=config["xvec_max_audio_seconds"],
        ),
        "implementation": {
            "upstream": dict(
                repository="https://github.com/studio-dots-ai/dots.tts",
                revision=revision,
                files_sha256={name: file_hash(source / name) for name in model.DOTS_FILES},
            ),
            "sources_sha256": {
                name: file_hash(Path(__file__).with_name(name + ".py"))
                for name in ("model", "profile", "storage")
            },
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("torch", "torchaudio", "numpy", "soundfile", "safetensors")
            },
            "python": platform.python_version(),
        },
        "timeline": timeline,
        "timeline_profile_id": digest(timeline),
        "waveform": {
            "order": [
                "decode_native_whole_sample",
                "mix_channels",
                "resample_48k",
                "normalize_edge_silence",
                "zero_pad_to_multiple",
            ],
            "channel_policy": "arithmetic_mean_float32",
            "sample_rate": model.SAMPLE_RATE,
            "resampling": dict(
                implementation="dots_tts.utils.audio.high_quality_resample (CPU float32)",
                resampling_method="sinc_interp_kaiser",
                lowpass_filter_width=64,
                rolloff=0.95,
            ),
            "edge_silence": dict(
                implementation="dots_tts normalize_edge_silence_duration",
                target_ms=250.0,
                top_db_below_peak=30.0,
                offsets="edge_leading_delta/edge_trailing_delta, signed 48 kHz samples",
            ),
            "align_multiple": model.ALIGN,
            "silent_audio": "failed silent_audio",
        },
        "latent": {
            "function": "AudioVAE.extract_latents (fp32, autocast off)",
            "stored": "posterior: mean = channels 0..127, logstd = channels 128..255",
            "frame_rate_hz": model.SAMPLE_RATE / model.HOP,
            "frames": "padded_frames / 1920",
            "lookahead_tail": f"{model.LOOKAHEAD_TAIL} zero samples after each waveform",
            "batch_semantics": "length-sorted zero-padded batches; every row ends in the tail",
            "max_batch_size": model.MAX_BATCH,
            "batch_samples": model.BATCH_SAMPLES,
            "normalization": "none (latent_stats.pt is applied by the trainer)",
        },
        "xvector": {
            "fbank": "dots_tts extract_speaker_fbank of the padded 48 kHz waveform "
            "(kaiser 48k->16k, 80 mel, dither 0, utterance mean norm)",
            "crop": "first 10 s of the padded waveform via SpeakerXVectorFeatures._crop_fbank "
            "(dots training crops a random 10 s window)",
            "model": "CAM++ 512, fp32",
        },
        "inference": {
            "precision": "float32",
            "tf32": False,
            "cudnn_benchmark": False,
            "cudnn_deterministic": True,
            "cuda": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
        },
        "output": dict(latent_dim=LATENT_DIM, xvector_dim=XVECTOR_DIM, storage_dtype="float32"),
    }


IDENTITY_KEYS = (
    "target_kind",
    "target_id",
    "parent_sample_id",
    "audio_sha256",
    "timeline_profile_id",
    "native_sample_rate",
    "start_frame",
    "end_frame",
    "profile_id",
)


def feature_row(row, definition, info, error, profile_id, posterior=None, xvector=None):
    identity = dict(
        task="audio-feature-v1",
        kind=KIND,
        target_kind="sample",
        target_id=row["sample_id"],
        parent_sample_id=row["sample_id"],
        audio_sha256=row["audio_sha256"],
        timeline_profile_id=definition["timeline_profile_id"],
        native_sample_rate=row["sample_rate"],
        start_frame=0,
        end_frame=info.get("end_frame", row["num_frames"]),
        profile_id=profile_id,
    )
    result = {k: identity[k] for k in IDENTITY_KEYS}
    result.update(
        input_fingerprint=digest(identity),
        feature_key=digest(
            [
                "feature-v1",
                row["audio_sha256"],
                definition["timeline_profile_id"],
                0,
                identity["end_frame"],
                profile_id,
            ]
        ),
        status="failed" if error else "ok",
        error_code=error,
        text=row["text"],
        text_kind=row["text_kind"],
        language=row["language"],
        resampled_frames=info.get("resampled_frames"),
        edge_leading_delta=info.get("edge_leading_delta"),
        edge_trailing_delta=info.get("edge_trailing_delta"),
        processed_frames=info.get("processed_frames"),
        padded_frames=info.get("padded_frames"),
        xvector_fbank_frames=info.get("xvector_fbank_frames"),
        num_latent_frames=None if posterior is None else int(posterior.shape[0]),
        latents_sha256=None
        if posterior is None
        else array_sha256(posterior, ["time", "posterior_channel"]),
        xvector_sha256=None if xvector is None else array_sha256(xvector, ["embedding"]),
    )
    return result


def validate_row(row, mean=None, logstd=None, xvector=None):
    identity = {k: row[k] for k in IDENTITY_KEYS}
    identity.update(task="audio-feature-v1", kind=KIND)
    key = [
        "feature-v1",
        row["audio_sha256"],
        row["timeline_profile_id"],
        row["start_frame"],
        row["end_frame"],
        row["profile_id"],
    ]
    if digest(identity) != row["input_fingerprint"] or digest(key) != row["feature_key"]:
        raise ValueError("Stored dots_vae identity mismatch")
    if row["status"] == "ok":
        frames = row["num_latent_frames"]
        posterior = np.concatenate([mean, logstd], axis=1) if mean is not None else None
        if (
            row["error_code"] is not None
            or posterior is None
            or posterior.shape != (frames, 2 * LATENT_DIM)
            or frames < 1
            or frames % 4
            or row["padded_frames"] != frames * model.HOP
            or row["padded_frames"] % model.ALIGN
            or row["processed_frames"]
            != row["resampled_frames"] + row["edge_leading_delta"] + row["edge_trailing_delta"]
            or not row["processed_frames"]
            <= row["padded_frames"]
            < row["processed_frames"] + model.ALIGN
            or not np.isfinite(posterior).all()
            or array_sha256(posterior, ["time", "posterior_channel"]) != row["latents_sha256"]
            or xvector is None
            or xvector.shape != (XVECTOR_DIM,)
            or not np.isfinite(xvector).all()
            or np.linalg.norm(xvector) == 0
            or array_sha256(xvector, ["embedding"]) != row["xvector_sha256"]
        ):
            raise ValueError(f"Invalid stored dots_vae result: {row['target_id']}")
    elif (
        row["status"] != "failed"
        or not row["error_code"]
        or mean is not None
        or xvector is not None
        or row["latents_sha256"] is not None
    ):
        raise ValueError("Invalid dots_vae failure record")
