"""BF16/FP32 RVQ with native norm order and official near-boundary rechecks."""

import torch
import triton
import triton.language as tl
from triton.language.extra.cuda import libdevice

from .linear import mm_kernel


def norm_height(m):
    if m == 1:
        return -64
    v = 4 if m % 4 == 0 else 2 if m % 2 == 0 else 1
    maximum = 512 // v
    dim = m // v
    power = 1 << (min(dim, maximum).bit_length() - 1)
    width = min(power, 32)
    return min(256, maximum // width)


@triton.jit
def native_norm(X, row, H: tl.constexpr, CONTIGUOUS: tl.constexpr = False):
    lane = tl.arange(0, H)
    a0 = tl.full((H,), 0, tl.float32)
    a1 = tl.full((H,), 0, tl.float32)
    a2 = tl.full((H,), 0, tl.float32)
    a3 = tl.full((H,), 0, tl.float32)
    for step in range(tl.cdiv(256, 4 * H)):
        if CONTIGUOUS:
            k0 = 4 * lane + step * 4 * H
            k1 = k0 + 1
            k2 = k0 + 2
            k3 = k0 + 3
        else:
            k0 = lane + step * 4 * H
            k1 = k0 + H
            k2 = k0 + 2 * H
            k3 = k0 + 3 * H
        v0 = tl.load(X + row * 256 + k0, k0 < 256, 0).to(tl.float32)
        v1 = tl.load(X + row * 256 + k1, k1 < 256, 0).to(tl.float32)
        v2 = tl.load(X + row * 256 + k2, k2 < 256, 0).to(tl.float32)
        v3 = tl.load(X + row * 256 + k3, k3 < 256, 0).to(tl.float32)
        a0 = a0 + v0 * v0
        a1 = a1 + v1 * v1
        a2 = a2 + v2 * v2
        a3 = a3 + v3 * v3
    value = ((a0 + a1) + a2) + a3
    if CONTIGUOUS:
        if H == 64:
            value = tl.where(lane < 32, value + tl.gather(value, (lane + 32) % H, axis=0), value)
        for level in tl.static_range(5):
            other = tl.gather(value, (lane + (1 << level)) % H, axis=0)
            value = tl.where(lane < 32, value + other, value)
    else:
        for level in tl.static_range(H.bit_length() - 1):
            offset = H >> (level + 1)
            other = tl.gather(value, (lane + offset) % H, axis=0)
            value = tl.where(lane < offset, value + other, value)
    return tl.sum(tl.where(lane == 0, value, 0.0), axis=0)


def dot_fp32(a, b):
    m, k = a.shape
    n = b.shape[1]
    out = torch.empty((m, n), device=a.device, dtype=torch.float32)
    mm_kernel[(triton.cdiv(m, 32), triton.cdiv(n, 64))](
        a, b, out, m, n, k, *a.stride(), *b.stride(), num_warps=4, num_stages=2
    )
    return out


@triton.jit(do_not_specialize=["M"])
def choose(
    DOT,
    XNORM,
    ENORM,
    X,
    E,
    CODES,
    R,
    M,
    D: tl.constexpr,
    V: tl.constexpr,
    MODE: tl.constexpr = 0,
    FLAGS=None,
    ULPS: tl.constexpr = -1,
    HEIGHTS=None,
    NATIVE_NORM: tl.constexpr = False,
    ERROR_SCALE: tl.constexpr = 0.0,
):
    row = tl.program_id(0)
    cols = tl.arange(0, V)
    dot = tl.load(DOT + row * V + cols)
    en = tl.load(ENORM + cols)
    if NATIVE_NORM:
        h = tl.load(HEIGHTS + row)
        if h < 0:
            xn = native_norm(X, row, 64, True)
        elif h == 4:
            xn = native_norm(X, row, 4)
        elif h == 8:
            xn = native_norm(X, row, 8)
        elif h == 16:
            xn = native_norm(X, row, 16)
        elif h == 32:
            xn = native_norm(X, row, 32)
        elif h == 64:
            xn = native_norm(X, row, 64)
        elif h == 128:
            xn = native_norm(X, row, 128)
        else:
            xn = native_norm(X, row, 256)
    else:
        xn = tl.load(XNORM + row)
    if MODE == 1 or MODE == 3:
        distance = ((-2 * dot) + xn) + en
    else:
        distance = (xn + en) - (2 * dot)
    squared = distance
    if MODE >= 2:
        distance = libdevice.sqrt_rn(tl.maximum(distance, 0.0))
    index = tl.argmin(distance, axis=0, tie_break_left=True)
    if ULPS >= 0:
        best = tl.min(distance, axis=0)
        second = tl.min(tl.where(cols == index, float("inf"), distance), axis=0)
        close = second.to(tl.int32, bitcast=True) - best.to(tl.int32, bitcast=True) <= ULPS
        if ERROR_SCALE > 0:
            selected = tl.sum(tl.where(cols == index, squared, 0.0), axis=0)
            bound = ERROR_SCALE * (2.0**-23) * (xn + en)
            close = close | (
                tl.max(tl.where(cols != index, bound - (squared - selected), -float("inf")), axis=0)
                >= 0
            )
        tl.store(FLAGS + row, tl.load(FLAGS + row) | close)
    tl.store(CODES + row, index)
    channels = tl.arange(0, D)
    value = tl.load(X + row * D + channels).to(tl.float32)
    embed = tl.load(E + index * D + channels)
    tl.store(R + row * D + channels, value - embed)


class Quantizer:
    """Semantic and acoustic residual chains retain the loaded model dtype."""

    def __init__(self, model, project):
        self.model = model
        self.project = project
        self.embed_cache = {}
        self.embedding_float_cache = {}
        self.norm_height_cache = {}
        self.guard_history = []

    def __call__(self, x, lengths):
        self.quant_lengths = lengths
        codes = self._quantize(x)
        return self._recheck(codes)

    def _quantize(self, x):
        all_codes = []
        flags = torch.zeros((16, len(x)), device=x.device, dtype=torch.bool)
        self.quantizer_residuals = []
        self.quantizer_layers = []
        key = (tuple(self.quant_lengths), x.device)
        if key not in self.norm_height_cache:
            if len(self.norm_height_cache) >= 64:
                self.norm_height_cache.clear()
            self.norm_height_cache[key] = torch.tensor(
                [norm_height(n) for n in self.quant_lengths for _ in range(n)],
                device=x.device,
                dtype=torch.int32,
            )
        heights = self.norm_height_cache[key]
        for rvq in [
            self.model.quantizer.semantic_residual_vector_quantizer,
            self.model.quantizer.acoustic_residual_vector_quantizer,
        ]:
            residual = x
            if rvq.input_proj is not None:
                residual = self.project(x, rvq.input_proj)
            count = (
                self.model.quantizer.num_semantic_quantizers
                if not all_codes
                else 16 - self.model.quantizer.num_semantic_quantizers
            )
            for layer in rvq.layers[:count]:
                self.quantizer_residuals.append(residual)
                self.quantizer_layers.append(layer)
                if id(layer) not in self.embedding_float_cache:
                    self.embedding_float_cache[id(layer)] = layer.codebook.embed.float()
                embed = self.embedding_float_cache[id(layer)]
                value = residual
                if id(layer) not in self.embed_cache:
                    self.embed_cache[id(layer)] = embed.square().sum(-1)
                dot = dot_fp32(residual, layer.codebook.embed.t())
                m, d = value.shape
                codes = torch.empty(m, device=x.device, dtype=torch.int64)
                next_residual = torch.empty_like(residual)
                # Preserve native distance order, rounded sqrt, and model-dtype residuals.
                # Near ties use the official codebook in _recheck.
                choose[(m,)](
                    dot,
                    None,
                    self.embed_cache[id(layer)],
                    value,
                    embed,
                    codes,
                    next_residual,
                    m,
                    d,
                    embed.shape[0],
                    3,
                    flags[len(all_codes)],
                    2,
                    heights,
                    True,
                    2.0,
                    num_warps=4,
                    enable_fp_fusion=False,
                )
                all_codes.append(codes)
                residual = next_residual
        self.quantizer_flags = flags
        return torch.stack(all_codes, dim=-1)

    def _recheck(self, out):
        flags = self.quantizer_flags.cpu().numpy()
        start = 0
        checked = []
        stages = 0
        for index, length in enumerate(self.quant_lengths):
            active = flags[:, start : start + length].any(axis=1)
            if active.any():
                checked.append(index)
                # Semantic and acoustic RVQs have independent residual chains.
                for begin, end in [(0, 1), (1, 16)]:
                    flagged = [j for j in range(begin, end) if active[j]]
                    if not flagged:
                        continue
                    first = flagged[0]
                    residual = (
                        self.quantizer_residuals[first][start : start + length].t().contiguous().t()
                    )
                    for j in range(first, end):
                        layer = self.quantizer_layers[j]
                        codes = layer.codebook.quantize(residual)
                        out[start : start + length, j] = codes
                        residual = (residual - layer.codebook.embed[codes]).t().contiguous().t()
                        stages += 1
            start += length
        self.guard_history.append(
            dict(
                samples=len(self.quant_lengths),
                rechecked=checked,
                native_stages=stages,
                ambiguous_rows=int(flags.sum()),
            )
        )
        self.quantizer_residuals = []
        self.quantizer_layers = []
        return out
