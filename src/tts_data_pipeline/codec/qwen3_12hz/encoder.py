"""Production BF16/FP32 packed encoder with no external padding."""

from pathlib import Path

from .audio import array_sha256, canonical, validate_codes, waveform
from .profile import feature_row, profile, verified_model_sources

__all__ = [
    "Encoder",
    "array_sha256",
    "canonical",
    "feature_row",
    "profile",
    "validate_codes",
    "verified_model_sources",
    "waveform",
]


class Encoder:
    """No external padding; fixed operator reductions and RVQ rechecks."""

    def __init__(self, model_root, gpu=0, *, precision="fp32"):
        if precision not in {"bf16", "fp32"}:
            raise ValueError("precision must be bf16 or fp32")
        import torch
        from qwen_tts import Qwen3TTSTokenizer

        from .packed.model import PackedModel

        torch.set_num_threads(1)
        torch.cuda.set_device(gpu)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = True
        torch.backends.cudnn.allow_tf32 = precision == "bf16"
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = False
        self.sources = verified_model_sources(model_root)
        self.device = f"cuda:{gpu}"
        self.tokenizer = Qwen3TTSTokenizer.from_pretrained(
            str(Path(model_root) / "codec"),
            device_map=self.device,
            dtype=torch.bfloat16 if precision == "bf16" else torch.float32,
            local_files_only=True,
        )
        self.tokenizer.model.eval()
        self.tokenizer.model.requires_grad_(False)
        self.runner = PackedModel(self)
        self.audit = {"samples": 0, "rechecked_samples": 0, "native_quantizer_stages": 0}

    def encode_single(self, wave):
        return self.encode_many([wave])[0]

    def encode_many(self, waves):
        if len(waves) > 64:
            raise ValueError("A packed batch must contain at most 64 samples")
        codes = self.runner.encode_many(waves)
        for batch in self.runner.guard_history:
            self.audit["samples"] += batch["samples"]
            self.audit["rechecked_samples"] += len(batch["rechecked"])
            self.audit["native_quantizer_stages"] += batch["native_stages"]
        # Bound memory over hundreds of millions of samples; retain cumulative audit.
        self.runner.guard_history.clear()
        return codes
