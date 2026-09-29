"""Qwen3 12 Hz BF16/FP32 forward pass over packed variable-length samples."""

import numpy as np
import torch
from transformers.models.mimi.modeling_mimi import apply_rotary_pos_emb

from ..audio import validate_codes
from .convolution import Convolution
from .linear import matmul
from .quantization import Quantizer


def efficient_attention(q, k, v, cq, ck, mq, mk, dropout_p, softmax_scale, causal, window_size):
    assert causal and window_size == (-1, -1) and dropout_p == 0
    return torch.ops.aten._efficient_attention_forward(
        q[None],
        k[None],
        v[None],
        None,
        cq,
        ck,
        mq,
        mk,
        0.0,
        1,
        False,
        scale=softmax_scale,
    )[0][0]


class PackedModel:
    def __init__(self, encoder):
        self.encoder = encoder
        self.model = encoder.tokenizer.model.encoder
        self.conv = Convolution(self.model)
        self.quantizer = Quantizer(self.model, self.project)
        self.guard_history = self.quantizer.guard_history
        self.qkv_weights = {
            id(layer.self_attn): torch.cat(
                [
                    layer.self_attn.q_proj.weight,
                    layer.self_attn.k_proj.weight,
                    layer.self_attn.v_proj.weight,
                ]
            )
            .t()
            .contiguous()
            for layer in self.model.encoder_transformer.layers
        }
        ropes = [layer.self_attn.rotary_emb for layer in self.model.encoder_transformer.layers]
        self.shared_rope = all(
            r.rope_type == "default"
            and torch.equal(r.inv_freq, ropes[0].inv_freq)
            and r.attention_scaling == ropes[0].attention_scaling
            for r in ropes
        )

    def linear_scaled(self, x, layer, scale, residual):
        out = matmul(x, layer.weight.t(), scale.scale, residual)
        offset = 0
        for length in self.linear_lengths:
            if length == 1:
                out[offset : offset + 1] = residual[offset : offset + 1] + scale(
                    layer(x[offset : offset + 1][None])[0]
                )
            offset += length
        return out

    def linear(self, x, layer):
        assert layer.bias is None and x.ndim == 2
        out = matmul(x, layer.weight.t())
        offset = 0
        for length in self.linear_lengths:
            if length == 1:
                out[offset : offset + 1] = layer(x[offset : offset + 1][None])[0]
            offset += length
        return out

    def project(self, x, layer):
        out = matmul(x, layer.weight[:, :, 0].t())
        offset = 0
        for length in self.quant_lengths:
            if length == 1:
                out[offset : offset + 1] = layer(x[offset : offset + 1].t()[None])[0].t()
            offset += length
        return out

    def qkv(self, x, attn):
        out = matmul(x, self.qkv_weights[id(attn)])
        offset = 0
        for length in self.linear_lengths:
            if length == 1:
                out[offset : offset + 1] = torch.cat(
                    [
                        layer(x[offset : offset + 1][None])[0]
                        for layer in [attn.q_proj, attn.k_proj, attn.v_proj]
                    ],
                    dim=-1,
                )
            offset += length
        return out.split(512, dim=-1)

    @torch.inference_mode()
    def encode_many(self, waves):
        if not waves:
            return []
        for w in waves:
            if np.asarray(w).ndim != 1 or not 0 < len(w) <= 2880000 or (not np.isfinite(w).all()):
                raise ValueError("invalid waveform")
        lengths = [len(w) for w in waves]
        x = torch.from_numpy(np.concatenate(waves).astype("float32")).to(
            self.encoder.device, dtype=next(self.model.parameters()).dtype
        )[:, None]
        for layer in self.model.encoder.layers:
            x, lengths = self.conv.layer(layer, x, lengths)
        self.linear_lengths = lengths
        cu = torch.tensor([0] + np.cumsum(lengths).tolist(), device=x.device, dtype=torch.int32)
        pos = torch.from_numpy(np.concatenate([np.arange(n, dtype=np.int64) for n in lengths])).to(
            x.device
        )[None, :]
        rotary_cache = None
        for layer in self.model.encoder_transformer.layers:
            attn = layer.self_attn
            h = layer.input_layernorm(x)
            n = len(x)
            q, k, v = self.qkv(h, attn)
            q = q.view(1, n, attn.num_heads, attn.head_dim).transpose(1, 2)
            k = k.view(1, n, attn.num_key_value_heads, attn.head_dim).transpose(1, 2)
            v = v.view(n, attn.num_key_value_heads, attn.head_dim)
            if rotary_cache is None:
                cos, sin = attn.rotary_emb(v, pos)
                if self.shared_rope:
                    rotary_cache = (cos, sin)
            else:
                cos, sin = rotary_cache
            q, k = apply_rotary_pos_emb(q, k, cos, sin)
            a = efficient_attention(
                q.transpose(1, 2)[0],
                k.transpose(1, 2)[0],
                v,
                cu,
                cu,
                3000,
                3000,
                dropout_p=0.0,
                softmax_scale=attn.scaling,
                causal=True,
                window_size=(-1, -1),
            )
            x = self.linear_scaled(a.reshape(n, -1), attn.o_proj, layer.self_attn_layer_scale, x)
            h = layer.post_attention_layernorm(x)
            h = layer.mlp.activation_fn(self.linear(h, layer.mlp.fc1))
            x = self.linear_scaled(h, layer.mlp.fc2, layer.mlp_layer_scale, x)
        y, sizes = self.conv.conv(self.model.downsample, x, lengths)
        self.quant_lengths = sizes
        codes = self.quantizer(y, sizes).cpu().numpy()
        return [
            validate_codes(c, len(w)) for c, w in zip(np.split(codes, np.cumsum(sizes)[:-1]), waves)
        ]
