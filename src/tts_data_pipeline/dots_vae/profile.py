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

PROFILE_NAME = "dots-tts-soar-vae-xvec-48k-fp32-v2"
# Profile name of each reference rule; the default rule keeps the original name.
PROFILE_NAMES = {
    "dots-reference-v1:codec-grid-v1-on-patches": PROFILE_NAME,
    "dots-reference-v2:min-3s-on-patches": "dots-tts-soar-vae-xvec-48k-fp32-v2-ref3s",
}
# v3: raw-caf-repair-v1 waveform (no edge silence normalization, embedded CAF header skipped).
RAW_PROFILE_NAMES = {
    "dots-reference-v1:codec-grid-v1-on-patches": "dots-tts-soar-vae-xvec-48k-fp32-v3",
    "dots-reference-v2:min-3s-on-patches": "dots-tts-soar-vae-xvec-48k-fp32-v3-ref3s",
}


def profile_name(reference_rule, waveform_policy=model.WAVEFORM_POLICY):
    if waveform_policy == model.WAVEFORM_POLICY:
        return PROFILE_NAMES[reference_rule]
    return RAW_PROFILE_NAMES[reference_rule]
REFERENCE_SPANS = {
    "dots-reference-v1:codec-grid-v1-on-patches": "N = num_latent_frames // 4 patches; "
    "lo = max(4, ceil(0.1 N)); hi = N // 2; lo > hi -> none; "
    "h = sha256('dots-reference-v1:' + target_id); L = lo + u64be(h[0:8]) % (hi - lo + 1); "
    "S = u64be(h[8:16]) % (N - L + 1); patches [S, S + L)",
    "dots-reference-v2:min-3s-on-patches": "N = num_latent_frames // 4 patches; "
    "lo = max(4, ceil(0.1 N)); hi = N // 2; lo > hi -> none; lo = max(lo, min(19, hi)); "
    "h = sha256('dots-reference-v2:' + target_id); L = lo + u64be(h[0:8]) % (hi - lo + 1); "
    "S = u64be(h[8:16]) % (N - L + 1); patches [S, S + L)",
}
KIND = "dots_vae"
LATENT_DIM = 128
XVECTOR_DIM = 512


def waveform_section(waveform_policy):
    section = {
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
    }
    if waveform_policy == model.WAVEFORM_POLICY:
        return section  # Unchanged: the published v2 profile ids depend on these bytes.
    section["policy"] = waveform_policy
    section["order"] = [
        "skip_embedded_caf_header",
        "decode_native_whole_sample",
        "mix_channels",
        "resample_48k",
        "zero_pad_to_multiple",
    ]
    section["edge_silence"] = dict(
        implementation="none: the recording's own leading/trailing audio is kept",
        offsets="edge_leading_delta = edge_trailing_delta = 0",
    )
    section["embedded_caf"] = dict(
        implementation="tts_data_pipeline.wav_repair.embedded_caf_header",
        file_sha256=file_hash(Path(__file__).parents[1] / "wav_repair.py"),
        rule="RIFF data chunk starting with 'caff': require desc lpcm, 16-bit little-endian "
        "integer, WAV rate and channels, 1 frame per packet; skip the CAF header bytes "
        "(through the CAF data chunk's edit count) as whole frames",
        identity="start_frame = skipped frames, end_frame = decoded native frames "
        "(native timeline of the WAV as decoded)",
    )
    return section


def profile(model_root, reference_rule=model.REFERENCE_RULE, waveform_policy=model.WAVEFORM_POLICY):
    if waveform_policy not in model.WAVEFORM_POLICIES:
        raise ValueError(f"Unknown waveform policy {waveform_policy}")
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
    result = {
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
        "waveform": waveform_section(waveform_policy),
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
        "reference_rule": reference_rule,
        "reference_xvector": {
            "span": REFERENCE_SPANS[reference_rule],
            "patch_samples": model.ALIGN,
            "audio": "edge-normalized 48 kHz waveform [S * 7680, min((S + L) * 7680, "
            "processed_frames)); the alignment pad is excluded",
            "fbank": "same as xvector",
            "crop": "none (whole span)",
            "model": "CAM++ 512, fp32, one sample at a time",
            "empty": "reference_start_patch, reference_end_patch and reference_xvector null "
            "without a span",
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
    if waveform_policy != model.WAVEFORM_POLICY:
        result["reference_xvector"]["audio"] = (
            "processed (not edge-normalized) 48 kHz waveform [S * 7680, min((S + L) * 7680, "
            "processed_frames)); the alignment pad is excluded"
        )
    return result


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


def feature_row(
    row, definition, info, error, profile_id, posterior=None, xvector=None, reference_xvector=None
):
    identity = dict(
        task="audio-feature-v1",
        kind=KIND,
        target_kind="sample",
        target_id=row["sample_id"],
        parent_sample_id=row["sample_id"],
        audio_sha256=row["audio_sha256"],
        timeline_profile_id=definition["timeline_profile_id"],
        native_sample_rate=row["sample_rate"],
        start_frame=info.get("start_frame", 0),
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
                identity["start_frame"],
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
        reference_start_patch=info.get("reference_start_patch"),
        reference_end_patch=info.get("reference_end_patch"),
        reference_fbank_frames=info.get("reference_fbank_frames"),
        reference_xvector_sha256=None
        if reference_xvector is None
        else array_sha256(reference_xvector, ["embedding"]),
    )
    return result


def valid_vector(vector, sha256):
    return (
        vector is not None
        and vector.shape == (XVECTOR_DIM,)
        and np.isfinite(vector).all()
        and np.linalg.norm(vector) != 0
        and array_sha256(vector, ["embedding"]) == sha256
    )


def validate_row(
    row, mean=None, logstd=None, xvector=None, reference_xvector=None, rule=model.REFERENCE_RULE
):
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
            or not valid_vector(xvector, row["xvector_sha256"])
        ):
            raise ValueError(f"Invalid stored dots_vae result: {row['target_id']}")
        span = model.reference_patches(row["target_id"], frames, rule)
        stored = (row["reference_start_patch"], row["reference_end_patch"])
        if span is not None:
            reference_ok = (
                stored == span
                and valid_vector(reference_xvector, row["reference_xvector_sha256"])
                and bool(row["reference_fbank_frames"])
            )
        else:
            reference_ok = stored == (None, None) and (
                reference_xvector is None
                and row["reference_xvector_sha256"] is None
                and row["reference_fbank_frames"] is None
            )
        if not reference_ok:
            raise ValueError(f"Invalid stored dots_vae result: {row['target_id']}")
    elif (
        row["status"] != "failed"
        or not row["error_code"]
        or mean is not None
        or xvector is not None
        or reference_xvector is not None
        or row["latents_sha256"] is not None
        or row["reference_start_patch"] is not None
    ):
        raise ValueError("Invalid dots_vae failure record")
