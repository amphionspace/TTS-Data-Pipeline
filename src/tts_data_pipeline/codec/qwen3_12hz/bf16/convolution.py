"""Packed causal CNN: sample boundaries, BF16 rounding and fixed split reductions."""

import numpy as np
import torch
import triton
import triton.language as tl
from transformers.models.mimi.modeling_mimi import MimiConv1d, MimiResnetBlock

from .linear import matmul


@triton.jit(do_not_specialize=["B"])
def bounds(rows, CI, CO, B):
    lo = tl.full(rows.shape, 0, tl.int32)
    hi = tl.full(rows.shape, B, tl.int32)
    for _ in range(7):
        mid = (lo + hi) // 2
        end = tl.load(CO + mid)
        right = end <= rows
        lo = tl.where(right, mid + 1, lo)
        hi = tl.where(right, hi, mid)
    sample = tl.minimum(lo - 1, B - 1)
    start = tl.load(CI + sample)
    stop = tl.load(CI + sample + 1)
    local = rows - tl.load(CO + sample)
    return start, stop, local


@triton.jit(do_not_specialize=["M", "B"])
def conv_kernel(
    X,
    W,
    BIAS,
    Y,
    CI,
    CO,
    M,
    CIN: tl.constexpr,
    COUT: tl.constexpr,
    KERNEL: tl.constexpr,
    STRIDE: tl.constexpr,
    DIL: tl.constexpr,
    LEFT: tl.constexpr,
    B,
    HAS_BIAS: tl.constexpr,
    REPLICATE: tl.constexpr,
    ROWS: tl.constexpr,
    BLOCK_K: tl.constexpr,
    SPATIAL_FIRST: tl.constexpr,
    CHANNEL_GROUP: tl.constexpr,
):
    rows = tl.program_id(0) * ROWS + tl.arange(0, ROWS)
    cols = tl.program_id(1) * 64 + tl.arange(0, 64)
    start, stop, local = bounds(rows, CI, CO, B)
    rk = tl.arange(0, BLOCK_K)
    acc = tl.full((ROWS, 64), 0, tl.float32)
    for step in range(tl.cdiv(CIN * KERNEL, BLOCK_K)):
        k = step * BLOCK_K + rk
        if CHANNEL_GROUP > 0:
            group = min(CIN, CHANNEL_GROUP)
            c = (k // (KERNEL * group)) * group + k % group
            kt = (k // group) % KERNEL
        elif SPATIAL_FIRST:
            c = k % CIN
            kt = k // CIN
        else:
            c = k // KERNEL
            kt = k % KERNEL
        offset = kt * DIL
        frame = start[:, None] + local[:, None] * STRIDE - LEFT + offset[None, :]
        if REPLICATE:
            frame = tl.minimum(tl.maximum(frame, start[:, None]), stop[:, None] - 1)
        good = (
            (rows[:, None] < M)
            & (k[None, :] < CIN * KERNEL)
            & (frame >= start[:, None])
            & (frame < stop[:, None])
        )
        a = tl.load(X + frame * CIN + c[None, :], good, 0)
        w = tl.load(
            W + cols[None, :] * (CIN * KERNEL) + (c * KERNEL + kt)[:, None],
            (cols[None, :] < COUT) & (k[:, None] < CIN * KERNEL),
            0,
        )
        acc = tl.dot(a, w, acc, input_precision="ieee")
    if HAS_BIAS:
        # Match native cuDNN convolution output cast before separate bias add.
        acc = acc.to(Y.dtype.element_ty).to(tl.float32)
        acc += tl.load(BIAS + cols, cols < COUT, 0)[None, :]
    tl.store(
        Y + rows[:, None] * COUT + cols[None, :], acc, (rows[:, None] < M) & (cols[None, :] < COUT)
    )


@triton.jit(do_not_specialize=["M", "B"])
def depthwise_kernel(
    X,
    W,
    BIAS,
    Y,
    CI,
    CO,
    M,
    C: tl.constexpr,
    K: tl.constexpr,
    S: tl.constexpr,
    D: tl.constexpr,
    L: tl.constexpr,
    B,
    HAS_BIAS: tl.constexpr,
    REPLICATE: tl.constexpr,
):
    idx = tl.program_id(0) * 256 + tl.arange(0, 256)
    rows = idx // C
    channels = idx % C
    start, stop, local = bounds(rows, CI, CO, B)
    acc = tl.full((256,), 0, tl.float32)
    for k in range(K):
        frame = start + local * S - L + k * D
        valid = (rows < M) & (frame >= start) & (frame < stop)
        if REPLICATE:
            frame = tl.minimum(tl.maximum(frame, start), stop - 1)
            valid = rows < M
        x = tl.load(X + frame * C + channels, valid, 0).to(tl.float32)
        w = tl.load(W + channels * K + k).to(tl.float32)
        acc = acc + x * w
    if HAS_BIAS:
        acc += tl.load(BIAS + channels).to(tl.float32)
    tl.store(Y + idx, acc, rows < M)


@triton.jit(do_not_specialize=["M", "B"])
def scalar_kernel(
    X, W, BIAS, Y, CI, CO, M, C: tl.constexpr, K: tl.constexpr, B, MODE: tl.constexpr
):
    idx = tl.program_id(0) * 256 + tl.arange(0, 256)
    rows = idx // C
    channel = idx % C
    start, stop, local = bounds(rows, CI, CO, B)
    acc = tl.full((256,), 0, tl.float64 if MODE == 2 else tl.float32)
    for step in tl.static_range(K):
        k = K - 1 - step if MODE == 1 else step
        frame = start + local - K + 1 + k
        x = tl.load(X + frame, (rows < M) & (frame >= start) & (frame < stop), 0).to(acc.dtype)
        w = tl.load(W + channel * K + k).to(acc.dtype)
        acc = tl.fma(x, w, acc)
    bias = tl.load(BIAS + channel).to(tl.float32)
    result = acc.to(Y.dtype.element_ty).to(tl.float32) + bias
    tl.store(Y + idx, result, rows < M)


def first(layer, x, lengths, boundary_cache, mode=0):
    c = layer.conv
    m = len(x)
    n = c.out_channels
    key = (tuple(lengths), 1, x.device)
    if key not in boundary_cache:
        import numpy as np

        cu = torch.tensor([0] + np.cumsum(lengths).tolist(), device=x.device, dtype=torch.int32)
        boundary_cache[key] = (cu, cu)
    ci, co = boundary_cache[key]
    out = torch.empty((m, n), device=x.device, dtype=x.dtype)
    assert c.in_channels == c.groups == c.stride[0] == c.dilation[0] == 1
    scalar_kernel[(triton.cdiv(m * n, 256),)](
        x, c.weight, c.bias, out, ci, co, m, n, c.kernel_size[0], len(lengths), mode
    )
    return out, lengths


@triton.jit(do_not_specialize=["M"])
def partial_kernel(
    X,
    W,
    BIAS,
    OUT,
    META,
    BORDERS,
    M,
    CIN: tl.constexpr,
    COUT: tl.constexpr,
    KERNEL: tl.constexpr,
    STRIDE: tl.constexpr,
    DIL: tl.constexpr,
    LEFT: tl.constexpr,
    REPLICATE: tl.constexpr,
    GROUP: tl.constexpr,
    SV: tl.constexpr,
    DIRECT: tl.constexpr,
    HAS_BIAS: tl.constexpr,
    ROWS: tl.constexpr,
):
    tile = tl.program_id(0)
    part = tl.program_id(2)
    begin = tl.load(META + tile * 2)
    end = tl.load(META + tile * 2 + 1)
    rows = begin + tl.arange(0, ROWS)
    cols = tl.program_id(1) * 64 + tl.arange(0, 64)
    rk = tl.arange(0, 32)
    acc = tl.full((ROWS, 64), 0, tl.float32)
    src = tl.load(BORDERS + rows * 3, rows < end, 0)
    stop = tl.load(BORDERS + rows * 3 + 1, rows < end, 0)
    out_start = tl.load(BORDERS + rows * 3 + 2, rows < end, 0)

    span = tl.cdiv(CIN, GROUP * SV) * GROUP * KERNEL
    for step in range(span // 32):
        k = (
            ((step * 32) // (GROUP * KERNEL) * SV + part) * (GROUP * KERNEL)
            + (step * 32) % (GROUP * KERNEL)
            + rk
        )
        c = (k // (KERNEL * GROUP)) * GROUP + k % GROUP
        kt = (k // GROUP) % KERNEL
        frame = (
            src[:, None] + (rows[:, None] - out_start[:, None]) * STRIDE - LEFT + kt[None, :] * DIL
        )
        if REPLICATE:
            frame = tl.minimum(tl.maximum(frame, src[:, None]), stop[:, None] - 1)
        a = tl.load(
            X + frame * CIN + c[None, :],
            (rows[:, None] < end)
            & (k[None, :] < CIN * KERNEL)
            & (frame >= src[:, None])
            & (frame < stop[:, None]),
            0,
        )
        w = tl.load(
            W + cols[None, :] * (CIN * KERNEL) + (c * KERNEL + kt)[:, None],
            (cols[None, :] < COUT) & (k[:, None] < CIN * KERNEL),
            0,
        )
        acc = tl.dot(a, w, acc, input_precision="ieee")
    if DIRECT and HAS_BIAS:
        acc = (
            acc.to(OUT.dtype.element_ty).to(tl.float32)
            + tl.load(BIAS + cols, cols < COUT, 0)[None, :]
        )
    tl.store(
        OUT + part * M * COUT + rows[:, None] * COUT + cols[None, :],
        acc,
        (rows[:, None] < end) & (cols[None, :] < COUT),
    )


@triton.jit(do_not_specialize=["M"])
def reduce_kernel(P, Y, BIAS, SPLITS, M, C: tl.constexpr, HAS_BIAS: tl.constexpr):
    idx = tl.program_id(0) * 256 + tl.arange(0, 256)
    rows = idx // C
    cols = idx % C
    sv = tl.load(SPLITS + rows, rows < M, 1)
    acc = tl.load(P + (sv - 1) * M * C + idx, (idx < M * C) & (sv > 1), 0)
    for part in tl.static_range(15):
        value = tl.load(P + part * M * C + idx, (idx < M * C) & (part < sv - 1) & (sv > 1), 0)
        acc = tl.where(part < sv - 1, acc + value, acc)
    if HAS_BIAS:
        acc = acc.to(Y.dtype.element_ty).to(tl.float32) + tl.load(BIAS + cols)
    tl.store(Y + idx, acc, (idx < M * C) & (sv > 1))


class Convolution:
    """Execute each CNN layer without reading across sample boundaries."""

    def __init__(self, model):
        self.boundary_cache = {}
        self.rows, self.block_k, self.warps, self.stages = 64, 32, 4, 2
        self.spatial_first = True
        self.channel_group = 0
        # Fixed kernel settings per operator; never selected from audio lengths.
        self.groups = {id(module): 32 for module in model.modules()}
        self.groups[id(model.encoder.layers[14])] = 64
        self.groups[id(model.downsample)] = 64
        # Split this large convolution into two fixed partial sums for parallelism.
        self.split_layer = id(model.encoder.layers[12])
        self.plan_cache = {}
        self.pointwise = {
            id(model.get_submodule(name))
            for name in ("encoder.layers.10.block.3", "encoder.layers.7.block.3")
        }

    def conv(self, layer, x, lengths):
        if id(layer) in self.pointwise:
            return self._pointwise(layer, x, lengths)
        if id(layer) == self.split_layer:
            return self._split(layer, x, lengths)
        if layer.conv.in_channels == 1 and layer.conv.kernel_size == (7,):
            return first(layer, x, lengths, self.boundary_cache)
        self.channel_group = self.groups[id(layer)]
        return self._ragged(layer, x, lengths)

    def _ragged(self, layer, x, lengths):
        c = layer.conv
        s = c.stride[0]
        k = c.kernel_size[0]
        d = c.dilation[0]
        outlengths = [(n + s - 1) // s for n in lengths]
        m = sum(outlengths)
        n = c.out_channels
        assert layer.causal and tuple(c.padding) == (0,)
        key = (tuple(lengths), s, x.device)
        if key not in self.boundary_cache:
            if len(self.boundary_cache) > 64:
                self.boundary_cache.clear()
            self.boundary_cache[key] = tuple(
                torch.tensor([0] + np.cumsum(ls).tolist(), device=x.device, dtype=torch.int32)
                for ls in [lengths, outlengths]
            )
        ci, co = self.boundary_cache[key]
        y = torch.empty((m, n), device=x.device, dtype=x.dtype)
        left = (k - 1) * d + 1 - s
        if c.groups == 1:
            assert layer.pad_mode in ["constant", "replicate"]
            conv_kernel[(triton.cdiv(m, self.rows), triton.cdiv(n, 64))](
                x,
                c.weight,
                c.bias,
                y,
                ci,
                co,
                m,
                c.in_channels,
                n,
                k,
                s,
                d,
                left,
                len(lengths),
                c.bias is not None,
                layer.pad_mode == "replicate",
                self.rows,
                self.block_k,
                self.spatial_first,
                self.channel_group,
                num_warps=self.warps,
                num_stages=self.stages,
            )
        else:
            assert c.groups == c.in_channels == n and layer.pad_mode in ["constant", "replicate"]
            depthwise_kernel[(triton.cdiv(m * n, 256),)](
                x,
                c.weight,
                c.bias,
                y,
                ci,
                co,
                m,
                n,
                k,
                s,
                d,
                left,
                len(lengths),
                c.bias is not None,
                layer.pad_mode == "replicate",
                enable_fp_fusion=False,
            )
        return y, outlengths

    def layer(self, layer, x, lengths):
        if isinstance(layer, MimiConv1d):
            return self.conv(layer, x, lengths)
        if isinstance(layer, MimiResnetBlock):
            residual, original = x, lengths
            for child in layer.block:
                x, lengths = self.layer(child, x, lengths)
            residual, other = self.layer(layer.shortcut, residual, original)
            assert other == lengths
            return residual + x, lengths
        return layer(x), lengths

    def _split(self, layer, x, lengths):
        c = layer.conv
        s = c.stride[0]
        sizes = [(n + s - 1) // s for n in lengths]
        strategies = [(32, 2)] * len(sizes)
        key = (id(layer), tuple(lengths), x.device)
        if key not in self.plan_cache:
            if len(self.plan_cache) >= 64:
                self.plan_cache.clear()
            groups = {}
            src = 0
            dst = 0
            spans = []
            borders = []
            for n, m, strategy in zip(lengths, sizes, strategies):
                if spans and spans[-1][0] == strategy:
                    spans[-1][2] += m
                else:
                    spans.append([strategy, dst, dst + m])
                borders.append((src, src + n, dst))
                src += n
                dst += m
            for strategy, start, stop in spans:
                for row in range(start, stop, 64):
                    groups.setdefault(strategy, []).append((row, stop))
            metas = [
                (st, torch.tensor(v, device=x.device, dtype=torch.int32))
                for st, v in groups.items()
            ]
            borders = torch.from_numpy(
                np.repeat(np.array(borders, dtype=np.int32), sizes, axis=0)
            ).to(x.device)
            splits = torch.from_numpy(
                np.repeat(np.array([st[1] for st in strategies], dtype=np.int32), sizes)
            ).to(x.device)
            self.plan_cache[key] = (metas, splits, max(st[1] for st in strategies), borders)

        metas, splits, maxs, borders = self.plan_cache[key]
        m = sum(sizes)
        assert maxs <= 16
        y = torch.empty((m, c.out_channels), device=x.device, dtype=x.dtype)
        partial = (
            torch.empty((maxs, m, c.out_channels), device=x.device, dtype=torch.float32)
            if maxs > 1
            else None
        )
        k = c.kernel_size[0]
        d = c.dilation[0]
        for (group, sv), meta in metas:
            partial_kernel[(len(meta), triton.cdiv(c.out_channels, 64), sv)](
                x,
                c.weight,
                c.bias,
                y if sv == 1 else partial,
                meta,
                borders,
                m,
                c.in_channels,
                c.out_channels,
                k,
                s,
                d,
                (k - 1) * d + 1 - s,
                layer.pad_mode == "replicate",
                group,
                sv,
                sv == 1,
                c.bias is not None,
                64,
                num_warps=4,
                num_stages=2,
            )
        if maxs > 1:
            reduce_kernel[(triton.cdiv(m * c.out_channels, 256),)](
                partial, y, c.bias, splits, m, c.out_channels, c.bias is not None, num_warps=4
            )
        return y, sizes

    def _pointwise(self, layer, x, lengths):
        c = layer.conv
        out = matmul(x, c.weight[:, :, 0].t())
        if c.bias is not None:
            out = out + c.bias
        offset = 0
        for length in lengths:
            if length == 1:
                out[offset : offset + 1] = c(x[offset : offset + 1].t()[None])[0].t()
            offset += length
        return out, lengths
