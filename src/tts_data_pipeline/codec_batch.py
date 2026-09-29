"""Auditioned C encoder: canonical batches, valid convolution ends, full causal FA2.

Batch shape depends only on a target's resampled length, never on its neighbors.
No OOM fallback may reduce batch shape: that would change the numerical profile.
"""

import types
from collections import defaultdict

import numpy as np
import torch
from flash_attn import flash_attn_func
from transformers.models.mimi.modeling_mimi import (
    MimiConv1d,
    MimiEuclideanCodebook,
    MimiFlashAttention2,
    MimiResnetBlock,
    apply_rotary_pos_emb,
)

from .codec_fast import install_integer_padding


def batch_shape(length):
    if length <= 0 or length > 120 * 24000:
        raise ValueError("C profile supports positive audio lengths up to 120 seconds")
    frames = (length + 48000 - 1) // 48000 * 48000
    limit = max(1, min(64, 480 * 24000 // frames))
    return frames, 2 ** (limit.bit_length() - 1)


def batch_indices(lengths):
    buckets = defaultdict(list)
    for i, n in enumerate(lengths):
        buckets[batch_shape(n)].append(i)
    for (frames, size), indices in sorted(buckets.items()):
        for start in range(0, len(indices), size):
            yield frames, size, indices[start : start + size]


def valid_mask(x, lengths):
    return x.masked_fill(
        torch.arange(x.shape[-1], device=x.device)[None, None, :]
        >= torch.tensor(lengths, device=x.device)[:, None, None],
        0,
    )


def conv(layer, x, lengths):
    if isinstance(layer, MimiConv1d):
        if layer.pad_mode == "constant":
            # A causal stride-one convolution never reads beyond the valid end.
            if not layer.causal or layer.conv.stride[0] != 1:
                x = valid_mask(x, lengths)
        elif layer.pad_mode == "replicate":
            ends = x[
                torch.arange(len(lengths), device=x.device),
                :,
                torch.tensor(lengths, device=x.device) - 1,
            ][:, :, None]
            invalid = (
                torch.arange(x.shape[-1], device=x.device)[None, None, :]
                >= torch.tensor(lengths, device=x.device)[:, None, None]
            )
            x = torch.where(invalid, ends, x)
        else:
            raise ValueError("Unsupported convolution padding mode")
        x = layer(x)
        stride = layer.conv.stride[0]
        return x, [(n + stride - 1) // stride for n in lengths]
    if isinstance(layer, MimiResnetBlock):
        residual, original = x, lengths
        for child in layer.block:
            x, lengths = conv(child, x, lengths)
        residual, shortcut_lengths = conv(layer.shortcut, residual, original)
        assert shortcut_lengths == lengths
        return residual + x, lengths
    return layer(x), lengths


def dense_flash(self, hidden_states, position_ids=None, **kwargs):
    if kwargs.get("attention_mask") is not None or kwargs.get("past_key_values") is not None:
        raise ValueError("Only right-padded causal non-streaming input supported")
    b, t, _ = hidden_states.shape
    q = self.q_proj(hidden_states).view(b, t, self.num_heads, self.head_dim).transpose(1, 2)
    k = (
        self.k_proj(hidden_states)
        .view(b, t, self.num_key_value_heads, self.head_dim)
        .transpose(1, 2)
    )
    v = (
        self.v_proj(hidden_states)
        .view(b, t, self.num_key_value_heads, self.head_dim)
        .transpose(1, 2)
    )
    cos, sin = self.rotary_emb(v, position_ids)
    q, k = apply_rotary_pos_emb(q, k, cos, sin)
    w = getattr(self, "sliding_window", None)
    window = (-1, -1) if w is None else (w - 1, w - 1)
    out = flash_attn_func(
        q.transpose(1, 2),
        k.transpose(1, 2),
        v.transpose(1, 2),
        dropout_p=0.0,
        softmax_scale=self.scaling,
        causal=self.is_causal,
        window_size=window,
    )
    return self.o_proj(out.reshape(b, t, -1)), None


def fixed(model, values, frames, size):
    actual = len(values)
    lengths = [v.numel() for v in values] + [1] * (size - actual)
    x = torch.zeros((size, 1, frames), device=values[0].device, dtype=values[0].dtype)
    for i, v in enumerate(values):
        x[i, 0, : len(v)] = v
    for layer in model.encoder.layers:
        x, lengths = conv(layer, x, lengths)
    # Causal attention never reads the invalid future or another example.
    x = model.encoder_transformer(x.transpose(1, 2), return_dict=True).last_hidden_state.transpose(
        1, 2
    )
    x, lengths = conv(model.downsample, x, lengths)
    # Preserve fixed dimensions for projection and all RVQ matmuls too.
    out = model.quantizer.encode(x, 16).permute(1, 2, 0).cpu().numpy().astype(np.int16)
    return [out[i, : lengths[i]] for i in range(actual)]


def install_c(model, device):
    for layer in model.encoder_transformer.layers:
        old = layer.self_attn
        replacement = MimiFlashAttention2(old.config, layer_idx=old.layer_idx)
        replacement.load_state_dict(old.state_dict(), strict=True)
        replacement.sliding_window = None
        replacement.forward = types.MethodType(dense_flash, replacement)
        layer.self_attn = replacement.to(device).eval()
    # The audition warmed FP32 before half(): Mimi's unregistered lazy _embed cache
    # remains FP32. Materialize it explicitly so a cold worker has identical semantics.
    for module in model.modules():
        if isinstance(module, MimiEuclideanCodebook):
            if module.embed_sum.dtype != torch.float32:
                raise ValueError("C codebook must be initialized from original FP32 weights")
            module._embed = module.embed.detach()
    model.half()
    model.encoder_transformer._attn_implementation = "flash_attention_2"
    model.config._attn_implementation = "flash_attention_2"
    install_integer_padding(model)
