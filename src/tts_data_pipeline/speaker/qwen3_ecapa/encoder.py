"""Unmodified Qwen ECAPA: FP32, equal-length batches, no external padding."""

import json
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import torch
from qwen_tts.core.models.configuration_qwen3_tts import Qwen3TTSConfig
from qwen_tts.core.models.modeling_qwen3_tts import Qwen3TTSSpeakerEncoder
from safetensors import safe_open

from .audio import decode, resampled_frames

MAX_BATCH = 8
MEL_FRAME_BUDGET = 22500
PREFETCH_SAMPLES = 16


def batch_indices(rows):
    """Group exact predicted mel lengths; verify actual shapes before every forward."""
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        samples = resampled_frames(row["num_frames"], row["sample_rate"])
        groups[samples // 256].append(i)
    for length, indices in sorted(groups.items()):
        size = min(MAX_BATCH, max(1, MEL_FRAME_BUDGET // max(1, length)))
        for offset in range(0, len(indices), size):
            yield indices[offset : offset + size]


class Encoder:
    def __init__(self, model_root, gpu, memory_fraction=0.15):
        torch.set_num_threads(1)
        torch.cuda.set_device(gpu)
        torch.cuda.set_per_process_memory_fraction(memory_fraction, gpu)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        self.device = torch.device("cuda", gpu)
        root = Path(model_root) / "speaker"
        config = Qwen3TTSConfig.from_dict(json.loads((root / "config.json").read_text()))
        self.dimension = config.speaker_encoder_config.enc_dim
        self.model = Qwen3TTSSpeakerEncoder(config.speaker_encoder_config).float().eval()
        with safe_open(root / "model.safetensors", framework="pt", device="cpu") as source:
            weights = {
                key.removeprefix("speaker_encoder."): source.get_tensor(key)
                for key in source.keys()
                if key.startswith("speaker_encoder.")
            }
        self.model.load_state_dict(weights, strict=True)
        self.model.requires_grad_(False).to(self.device)
        device = torch.cuda.get_device_properties(gpu)
        self.hardware = dict(
            name=device.name, gpu=gpu, cuda=torch.version.cuda, cudnn=torch.backends.cudnn.version()
        )

    @torch.inference_mode()
    def encode(self, mels):
        if not mels:
            return []
        shape = mels[0].shape
        if len(shape) != 2 or shape[0] < 5 or shape[1] != 128:
            raise ValueError("Invalid native speaker mel shape")
        if any(m.shape != shape or m.dtype != torch.float32 for m in mels):
            raise ValueError("Speaker batches must have identical lengths and FP32 dtype")
        output = self.model(torch.stack(mels).to(self.device)).cpu().numpy()
        if output.shape != (len(mels), self.dimension) or output.dtype != np.float32:
            raise ValueError("Unexpected speaker output")
        if not np.isfinite(output).all() or np.any(np.linalg.norm(output, axis=1) == 0):
            raise ValueError("Invalid speaker embedding")
        return output

    def extract(self, rows, decoders):
        """Bounded CPU prefetch, preserving input order and individual failure records."""
        iterator = iter(batch_indices(rows))
        queue = deque()
        results = [None] * len(rows)

        def fill():
            while sum(len(indices) for indices, _ in queue) < PREFETCH_SAMPLES:
                indices = next(iterator, None)
                if indices is None:
                    break
                queue.append((indices, [decoders.submit(decode, rows[i]) for i in indices]))

        fill()
        while queue:
            indices, futures = queue.popleft()
            decoded = [future.result() for future in futures]
            valid = [
                (i, mel, info) for i, (mel, info, error) in zip(indices, decoded) if error is None
            ]
            fill()
            # Container frame counts are only scheduling hints.
            # Re-group by actual mel shape; never pad or trim to the prediction.
            actual_groups = defaultdict(list)
            for item in valid:
                actual_groups[item[1].shape].append(item)
            for shape, group in actual_groups.items():
                size = min(MAX_BATCH, max(1, MEL_FRAME_BUDGET // shape[0]))
                for offset in range(0, len(group), size):
                    batch = group[offset : offset + size]
                    output = self.encode([mel for _, mel, _ in batch])
                    for (i, _, info), vector in zip(batch, output, strict=True):
                        results[i] = (info, vector, None)
            for i, (_, info, error) in zip(indices, decoded, strict=True):
                if error:
                    results[i] = (info, None, error)
        return results
