"""BF16 packed profile, pinned model sources, and feature-row identity."""

import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

import soundfile as sf

from .audio import array_sha256, canonical


def verified_model_sources(model_root):
    root = Path(model_root)
    source = json.loads((root / "sources.json").read_text())["codec"]
    for name, expected in source["files"].items():
        with (root / "codec" / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Model source changed: {name}")
    return source


def profile(model_root):
    """Materialize the BF16 implementation; acceptance is separate."""
    import inspect

    import qwen_tts.core.tokenizer_12hz.modeling_qwen3_tts_tokenizer_v2 as model
    import qwen_tts.inference.qwen3_tts_tokenizer as wrapper
    import scipy
    import torch
    import transformers.models.mimi.modeling_mimi as mimi

    sources = verified_model_sources(model_root)
    deps = {
        name: importlib.metadata.version(name)
        for name in (
            "torch",
            "transformers",
            "qwen-tts",
            "numpy",
            "scipy",
            "soundfile",
            "flash-attn",
            "triton",
        )
    }
    deps.update(python=platform.python_version(), libsndfile=sf.__libsndfile_version__)
    timeline = {
        "decoder": "soundfile",
        "soundfile_version": sf.__version__,
        "libsndfile_version": sf.__libsndfile_version__,
        "output_rate": "native",
        "output_channels": "native",
        "dtype": "float32",
        "length_policy": "exact_match_base_frames",
        "implicit_pad_or_trim": "none",
    }
    files = {
        name: hashlib.sha256(Path(inspect.getfile(module)).read_bytes()).hexdigest()
        for name, module in [("wrapper", wrapper), ("model", model), ("mimi", mimi)]
    }
    files["frontend"] = hashlib.sha256(
        Path(__file__).with_name("audio.py").read_bytes()
    ).hexdigest()
    # Hash the complete numerical path; historical runs retain frozen source.
    for name in ("encoder", "profile", "schedule"):
        files[name] = hashlib.sha256(
            Path(__file__).with_name(f"{name}.py").read_bytes()
        ).hexdigest()
    for path in sorted(Path(__file__).with_name("bf16").glob("*")):
        if path.suffix in {".py", ".json"}:
            files[f"bf16/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "kind": "audio_codec",
        "architecture": "Qwen3-TTS-Tokenizer-12Hz",
        "model": {
            **sources,
            "config": json.loads((Path(model_root) / "codec/config.json").read_text()),
        },
        "implementation": {"sources_sha256": files, "dependencies": deps},
        "timeline": timeline,
        "timeline_profile_id": hashlib.sha256(canonical(timeline)).hexdigest(),
        "waveform": {
            "order": ["decode_native", "crop_native", "mix_channels", "resample"],
            "channel_policy": "arithmetic_mean_float32",
            "sample_rate": 24000,
            "resampling": {
                "implementation": "scipy.signal.resample_poly",
                "version": scipy.__version__,
                "up_down": "reduce_by_gcd",
                "window": ["kaiser", 5.0],
                "padtype": "constant",
                "cval": 0.0,
                "length": "ceil(native_crop_frames * 24000 / native_sample_rate)",
                "same_rate": "identity",
            },
            "normalization": "none",
            "silence_trim": "none",
            "augmentation": "none",
        },
        "inference": {
            "eval": True,
            "precision": "bfloat16",
            "weight_loading": "official_from_pretrained_dtype_bfloat16",
            "distance_math": "float32_native_norm_and_euclidean_distance_order",
            "codebook_cache": "official_bfloat16_embed_then_cast_float32",
            "residual_math": "bfloat16_each_stage",
            "attention_backend": "aten_efficient_attention_forward_cutlass",
            "attention_window": "full_causal_per_sample",
            "autocast": False,
            "tf32": False,
            "cudnn_allow_tf32": True,
            "cudnn_benchmark": False,
            "cudnn_deterministic": False,
            "batch_semantics": "packed_actual_lengths_fixed_operator_reductions",
            "max_batch_size": 64,
            "scheduling_audio_seconds_budget": 480,
            "max_input_frames": 2880000,
            "requested_encoder_quantizers": 16,
            "external_padding": "none",
            "intrinsic_padding": "official_causal_stride_and_replicate_downsample",
            "quantizer_guard": {
                "distance_ulps": 2,
                "squared_distance_scale_eps": 2.0,
                "recheck": "official_codebook_remaining_residual_chain",
            },
            "length_dependent_policies": False,
            "validated_hardware": "NVIDIA A800-SXM4-80GB",
            "cuda": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(),
            "chunking": "none",
            "oom_policy": "fail_no_numerical_fallback",
        },
        "output": {
            "axes": ["time", "codebook"],
            "num_codebooks": 16,
            "vocab_sizes": [2048] * 16,
            "storage_dtype": "int16",
            "special_tokens": "none",
            "length_rule": "ceil(encoder_input_num_frames / 1920); verified against returned shape",
        },
        "reproducibility": {
            "comparator": "exact_integer",
            "official_reference": "unmodified_single_actual_length_same_dtype",
            "official_bitwise_equivalence": False,
            "floating_point_order": "fixed_kernels_may_differ_from_native_backend",
        },
    }


def feature_row(row, definition, wave=None, codes=None, error=None):
    pid = hashlib.sha256(canonical(definition)).hexdigest()
    identity = {
        "task": "audio-feature-v1",
        "kind": "codec",
        "target_kind": "sample",
        "target_id": row["sample_id"],
        "parent_sample_id": row["sample_id"],
        "audio_sha256": row["audio_sha256"],
        "timeline_profile_id": definition["timeline_profile_id"],
        "native_sample_rate": row["sample_rate"],
        "start_frame": 0,
        "end_frame": row["num_frames"],
        "profile_id": pid,
    }
    result = {k: v for k, v in identity.items() if k not in {"task", "kind"}}
    result.update(
        input_fingerprint=hashlib.sha256(canonical(identity)).hexdigest(),
        feature_key=hashlib.sha256(
            canonical(
                [
                    "feature-v1",
                    row["audio_sha256"],
                    definition["timeline_profile_id"],
                    0,
                    row["num_frames"],
                    pid,
                ]
            )
        ).hexdigest(),
        status="failed" if error else "ok",
        error_code=error,
        num_codebooks=16,
        encoder_input_num_frames=None if wave is None else len(wave),
        encoder_input_sha256=None if wave is None else array_sha256(wave, ["sample"]),
        num_codec_frames=None if codes is None else len(codes),
        codes_sha256=None if codes is None else array_sha256(codes, ["time", "codebook"]),
        codes=None if codes is None else codes.tolist(),
    )
    return result
