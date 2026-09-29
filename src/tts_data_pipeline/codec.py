"""Codec waveform identity, C numerical profile, and fixed-shape FP16/FA2 inference."""

import hashlib
import importlib.metadata
import io
import json
import math
import platform
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def array_sha256(array, axes):
    array = np.asarray(array)
    if array.dtype.kind not in "fi" or array.ndim != len(axes):
        raise ValueError("Unsupported canonical array")
    data = np.ascontiguousarray(array, dtype=array.dtype.newbyteorder("<"))
    header = {"dtype": data.dtype.name, "shape": list(data.shape), "axes": axes}
    return hashlib.sha256(canonical(header) + b"\n" + data.tobytes()).hexdigest()


def waveform(row):
    """Decode the entire base sample; source recording timestamps are not crop coordinates."""
    data = row["audio"]["bytes"]
    if not data or hashlib.sha256(data).hexdigest() != row["audio_sha256"]:
        raise ValueError("audio_sha256_mismatch")
    audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    if (
        sr != row["sample_rate"]
        or len(audio) != row["num_frames"]
        or audio.shape[1] != row["channels"]
    ):
        raise ValueError("native_timeline_mismatch")
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
    return mono


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


def verified_model_sources(model_root):
    root = Path(model_root)
    source = json.loads((root / "sources.json").read_text())["codec"]
    for name, expected in source["files"].items():
        with (root / "codec" / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Model source changed: {name}")
    return source


def profile(model_root):
    """Materialize the complete C candidate; acceptance is separate."""
    import inspect

    import qwen_tts.core.tokenizer_12hz.modeling_qwen3_tts_tokenizer_v2 as model
    import qwen_tts.inference.qwen3_tts_tokenizer as wrapper
    import scipy
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
    files["frontend"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    files["execution"] = hashlib.sha256(
        Path(__file__).with_name("codec_fast.py").read_bytes()
    ).hexdigest()
    files["batch"] = hashlib.sha256(
        Path(__file__).with_name("codec_batch.py").read_bytes()
    ).hexdigest()
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
            "precision": "float16",
            "weight_loading": "load_float32_materialize_codebook_cache_then_cast_encoder",
            "distance_math": "float32_on_cached_fp32_codebooks",
            "codebook_cache": "embed_sum_fp32 / clamp(cluster_usage_fp32, min=1e-5)",
            "residual_math": "first_input_fp16_then_promoted_float32_after_decode_subtraction",
            "attention_backend": "flash_attention_2",
            "attention_window": "full_causal",
            "autocast": False,
            "tf32": False,
            "randomness": "none",
            "cudnn_benchmark": False,
            "cudnn_deterministic": True,
            "batch_semantics": "canonical_shape_from_target_length",
            "bucket_frames": 48000,
            "max_batch_size": 64,
            "padded_audio_seconds_budget": 480,
            "batch_size_rule": "floor_power_of_two(min(64, floor(480*24000/bucket_frames)))",
            "tail_policy": "fill_unused_slots_with_zero_waveform_length_1",
            "max_input_frames": 2880000,
            "requested_encoder_quantizers": 16,
            "padding_and_valid_lengths": "per_layer_mask_and_replicate_valid_downsample_end",
            "padding_shape_arithmetic": "python_integer_non_streaming",
            "cuda_graphs": None,
            "chunking": "none",
            "oom_policy": "fail_no_shape_fallback",
        },
        "output": {
            "axes": ["time", "codebook"],
            "num_codebooks": 16,
            "vocab_sizes": [2048] * 16,
            "storage_dtype": "int16",
            "special_tokens": "none",
            "length_rule": "ceil(encoder_input_num_frames / 1920); verified against returned shape",
        },
        "reproducibility": {"comparator": "exact_integer"},
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


class Encoder:
    """Only C is a production encoder; decoder stays FP32 for listening comparisons."""

    def __init__(self, model_root, gpu=0):
        import torch
        from qwen_tts import Qwen3TTSTokenizer
        from qwen_tts.core import Qwen3TTSTokenizerV2Config

        from .codec_batch import install_c

        self.sources = verified_model_sources(model_root)
        torch.set_num_threads(1)
        torch.cuda.set_device(gpu)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        self.device = f"cuda:{gpu}"
        config = Qwen3TTSTokenizerV2Config.from_pretrained(
            str(Path(model_root) / "codec"), local_files_only=True
        )
        config._attn_implementation = "sdpa"
        config.encoder_config._attn_implementation = "sdpa"
        config.decoder_config._attn_implementation = "sdpa"
        self.tokenizer = Qwen3TTSTokenizer.from_pretrained(
            str(Path(model_root) / "codec"),
            device_map=self.device,
            dtype=torch.float32,
            config=config,
            attn_implementation={"": "sdpa", "encoder_config": "sdpa", "decoder_config": "sdpa"},
            local_files_only=True,
        )
        self.tokenizer.model.eval()
        self.tokenizer.model.requires_grad_(False)
        install_c(self.tokenizer.model.encoder, self.device)
        self.attention_classes = [
            type(layer.self_attn).__name__
            for layer in self.tokenizer.model.encoder.encoder_transformer.layers
        ]
        if (
            self.attention_classes
            != ["MimiFlashAttention2"] * config.encoder_config.num_hidden_layers
        ):
            raise RuntimeError(f"Actual attention classes disagree: {self.attention_classes}")

    def encode_single(self, wave):
        return self.encode_many([wave])[0]

    def encode_many(self, waves):
        import torch

        from .codec_batch import batch_indices, fixed

        results = [None] * len(waves)
        with torch.inference_mode():
            for frames, size, indices in batch_indices([len(w) for w in waves]):
                values = [
                    torch.from_numpy(np.ascontiguousarray(waves[i]))
                    .pin_memory()
                    .to(device=self.device, dtype=torch.float16, non_blocking=True)
                    for i in indices
                ]
                codes = fixed(self.tokenizer.model.encoder, values, frames, size)
                for i, c in zip(indices, codes, strict=True):
                    results[i] = validate_codes(c, len(waves[i]))
        return results

    def decode(self, codes):
        import torch

        values, sr = self.tokenizer.decode(
            {"audio_codes": [torch.from_numpy(np.asarray(codes, dtype=np.int64))]}
        )
        value = values[0]
        if sr != 24000 or not np.isfinite(value).all() or len(value) != len(codes) * 1920:
            raise ValueError("Invalid reconstructed waveform")
        return value
