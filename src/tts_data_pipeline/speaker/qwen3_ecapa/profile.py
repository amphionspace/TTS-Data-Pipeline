"""Frozen speaker weights, full frontend semantics, and feature identities."""

import hashlib
import importlib.metadata
import inspect
import json
import platform
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts.core.models import modeling_qwen3_tts as native
from safetensors import safe_open

from ...feature_audio import array_sha256
from ...feature_runtime import file_hash
from ...schema import digest
from .audio import MEL_OPTIONS
from .encoder import MAX_BATCH, MEL_FRAME_BUDGET

PROFILE_NAME = "qwen3-ecapa-24k-fp32-packed-v1"


def native_profile(model_root):
    root = Path(model_root)
    source = json.loads((root / "sources.json").read_text())["speaker"]
    for name in ("config.json", "model.safetensors"):
        if file_hash(root / "speaker" / name) != source["files"][name]:
            raise ValueError(f"Speaker model source changed: {name}")
    config = json.loads((root / "speaker/config.json").read_text())["speaker_encoder_config"]
    tensors = {}
    with safe_open(root / "speaker/model.safetensors", framework="pt", device="cpu") as weights:
        for name in weights.keys():
            if name.startswith("speaker_encoder."):
                tensor = weights.get_tensor(name).contiguous()
                tensors[name] = dict(
                    shape=list(tensor.shape),
                    dtype=str(tensor.dtype),
                    sha256=hashlib.sha256(tensor.view(torch.uint8).numpy().tobytes()).hexdigest(),
                )
    timeline = dict(
        decoder="soundfile",
        soundfile_version=sf.__version__,
        libsndfile_version=sf.__libsndfile_version__,
        output_rate="native",
        output_channels="native",
        dtype="float32",
        length_policy="use_complete_actual_decode",
        metadata_frame_count="scheduling_hint_only",
        implicit_pad_or_trim="none",
    )
    return {
        "kind": "speaker_embedding",
        "architecture": "Qwen-ECAPA-TDNN",
        "model": dict(
            repo_id=source["repo_id"], revision=source["revision"], config=config, tensors=tensors
        ),
        "implementation": {
            "sources_sha256": {
                **{
                    name: file_hash(Path(__file__).with_name(name + ".py"))
                    for name in ("audio", "encoder", "profile")
                },
                "native": file_hash(inspect.getfile(native)),
            },
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in (
                    "torch",
                    "torchaudio",
                    "qwen-tts",
                    "transformers",
                    "numpy",
                    "soundfile",
                    "librosa",
                    "safetensors",
                )
            },
            "python": platform.python_version(),
        },
        "timeline": timeline,
        "timeline_profile_id": digest(timeline),
        "waveform": {
            "order": ["decode_native_whole_sample", "mix_channels", "resample"],
            "channel_policy": "arithmetic_mean_float32",
            "sample_rate": 24000,
            "resampling": dict(
                implementation="torchaudio.functional.resample",
                resampling_method="sinc_interp_hann",
                lowpass_filter_width=6,
                rolloff=0.99,
                beta=None,
                same_rate="identity",
                length="native torchaudio FP32 ceil, without correction",
            ),
            "normalization": "none",
            "silence_trim": "none",
            "augmentation": "none",
        },
        "frontend": {
            **MEL_OPTIONS,
            "function": "qwen_tts.mel_spectrogram",
            "intrinsic_padding": "384 samples per side, reflect",
            "window": "hann_periodic",
            "magnitude": "sqrt(real^2+imag^2+1e-9)",
            "mel": "librosa Slaney normalization, htk=False",
            "compression": "natural_log(clamp_min(1e-5))",
            "persistent_mel_required": False,
        },
        "inference": {
            "precision": "float32",
            "weight_loading": "checkpoint_values_cast_to_float32",
            "eval": True,
            "autocast": False,
            "tf32": False,
            "cudnn_benchmark": False,
            "cudnn_deterministic": True,
            "batch_semantics": "strict_equal_actual_mel_lengths_else_single",
            "external_padding": "none",
            "intrinsic_padding": "unmodified_native_reflection",
            "max_batch_size": MAX_BATCH,
            "mel_frame_budget": MEL_FRAME_BUDGET,
            "min_waveform_frames": 1280,
            "min_mel_frames": 5,
            "chunking": "none",
            "truncation": "none",
            "oom_policy": "stop_and_resume",
            "cuda": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
        },
        "output": dict(
            embedding_dim=config["enc_dim"],
            storage_dtype="float32",
            layer="speaker_encoder_output_before_talker_injection",
            pooling="native_attentive_statistics",
            normalization="none",
            zero_norm="reject",
        ),
        "reproducibility": dict(
            comparator="elementwise_atol_rtol",
            atol=1e-5,
            rtol=1e-4,
            reference="unmodified_single_sample_fp32",
            official_bitwise_equivalence=False,
        ),
    }


def feature_row(
    row, definition, info, embedding=None, error=None, *, profile_id=None, include_embedding=True
):
    pid = profile_id or digest(definition)
    identity = dict(
        task="audio-feature-v1",
        kind="speaker_embedding",
        target_kind="sample",
        target_id=row["sample_id"],
        parent_sample_id=row["sample_id"],
        audio_sha256=row["audio_sha256"],
        timeline_profile_id=definition["timeline_profile_id"],
        native_sample_rate=row["sample_rate"],
        start_frame=0,
        end_frame=info.get("end_frame", row["num_frames"]),
        profile_id=pid,
    )
    result = {key: value for key, value in identity.items() if key not in {"task", "kind"}}
    result.update(
        input_fingerprint=digest(identity),
        feature_key=digest(
            [
                "feature-v1",
                row["audio_sha256"],
                definition["timeline_profile_id"],
                0,
                identity["end_frame"],
                pid,
            ]
        ),
        status="failed" if error else "ok",
        error_code=error,
        embedding_dim=definition["output"]["embedding_dim"],
        **info,
        embedding=None if embedding is None or not include_embedding else embedding.tolist(),
        embedding_sha256=None if embedding is None else array_sha256(embedding, ["embedding"]),
    )
    return result


def validate_result(row):
    identity = {
        key: row[key]
        for key in (
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
    }
    identity.update(task="audio-feature-v1", kind="speaker_embedding")
    key = [
        "feature-v1",
        row["audio_sha256"],
        row["timeline_profile_id"],
        row["start_frame"],
        row["end_frame"],
        row["profile_id"],
    ]
    if digest(identity) != row["input_fingerprint"] or digest(key) != row["feature_key"]:
        raise ValueError("Stored speaker identity mismatch")
    if row["status"] == "ok":
        vector = np.asarray(row["embedding"], dtype=np.float32)
        if (
            vector.shape != (row["embedding_dim"],)
            or not np.isfinite(vector).all()
            or np.linalg.norm(vector) == 0
            or row["error_code"] is not None
            or row["encoder_input_num_frames"] < 1280
            or not row["encoder_input_sha256"]
            or array_sha256(vector, ["embedding"]) != row["embedding_sha256"]
        ):
            raise ValueError("Invalid stored speaker embedding")
    elif (
        row["status"] != "failed"
        or not row["error_code"]
        or row["embedding"] is not None
        or row["embedding_sha256"] is not None
    ):
        raise ValueError("Invalid speaker failure record")


def profile(model_root):
    """Production numerical profile; launch/resume binds exact implementation and libraries."""
    from .decoder import DECODER_LIBRARIES
    from .packed_encoder import MAX_BATCH, MEL_FRAME_BUDGET

    definition = native_profile(model_root)
    for name in ("decoder", "frontend", "mel", "packed", "packed_encoder", "storage"):
        definition["implementation"]["sources_sha256"][name] = file_hash(
            Path(__file__).with_name(name + ".py")
        )
    definition["implementation"]["dependencies"]["triton"] = importlib.metadata.version("triton")
    definition["timeline"]["decoder"] = (
        "libsndfile_memfd; system Opus, packaged library for other formats"
    )
    definition["timeline"]["decoder_libraries_sha256"] = {
        name: file_hash(name) for name in DECODER_LIBRARIES
    }
    definition["timeline_profile_id"] = digest(definition["timeline"])
    definition["waveform"]["resampling"]["implementation"] = (
        "cached torchaudio.transforms.Resample(dtype=float32), bitwise native functional kernel"
    )
    definition["frontend"]["function"] = (
        "CUDA FP32 rfft on actual STFT windows and FP32 mel projection"
    )
    definition["frontend"]["cpu_bitwise_equivalent"] = False
    definition["inference"].pop("tf32")
    definition["inference"].update(
        torch_allow_tf32=False,
        matrix_multiply="tf32x3_compensated_float32",
        final_projection="original_fp32_conv1d",
        batch_semantics="concatenate_actual_frames_with_per_sample_offsets",
        max_batch_size=MAX_BATCH,
        mel_frame_budget=MEL_FRAME_BUDGET,
    )
    return definition
