"""FP32 ECAPA on concatenated actual mel frames; no external padding."""

import torch
import triton
import triton.language as tl


@triton.jit(do_not_specialize=["M", "B"])
def conv(
    X,
    W,
    BIAS,
    Y,
    OFF,
    M,
    B,
    CI: tl.constexpr,
    CO: tl.constexpr,
    K: tl.constexpr,
    D: tl.constexpr,
    P: tl.constexpr,
    RELU: tl.constexpr,
):
    r = tl.program_id(0) * 32 + tl.arange(0, 32)
    c = tl.program_id(1) * 64 + tl.arange(0, 64)
    kk = tl.arange(0, 32)
    if K > 1:
        lo = tl.full((32,), 0, tl.int32)
        hi = tl.full((32,), B, tl.int32)
        for _ in range(7):
            mid = (lo + hi) // 2
            end = tl.load(OFF + mid)
            right = end <= r
            lo = tl.where(right, mid + 1, lo)
            hi = tl.where(right, hi, mid)
        sample = tl.minimum(lo - 1, B - 1)
        start = tl.load(OFF + sample)
        stop = tl.load(OFF + sample + 1)
        length = stop - start
    acc = tl.full((32, 64), 0, tl.float32)
    for step in range(tl.cdiv(CI * K, 32)):
        q = step * 32 + kk
        ic = q % CI
        kt = q // CI
        if K > 1:
            t = (r - start)[:, None] + kt[None, :] * D - P
            t = tl.where(t < 0, -t, t)
            t = tl.where(t >= length[:, None], 2 * length[:, None] - 2 - t, t)
            frame = start[:, None] + t
        else:
            frame = r[:, None]
        a = tl.load(X + frame * CI + ic[None, :], (r[:, None] < M) & (q[None, :] < CI * K), 0)
        w = tl.load(W + q[:, None] * CO + c[None, :], (q[:, None] < CI * K) & (c[None, :] < CO), 0)
        acc = tl.dot(a, w, acc, input_precision="tf32x3")
    acc = acc + tl.load(BIAS + c, c < CO, 0)[None, :]
    if RELU:
        acc = tl.maximum(acc, 0.0)
    tl.store(Y + r[:, None] * CO + c[None, :], acc, (r[:, None] < M) & (c[None, :] < CO))


@triton.jit
def stats_kernel(X, W, OFF, MEAN, STD, C: tl.constexpr, MODE: tl.constexpr):
    b = tl.program_id(0)
    c = tl.program_id(1) * 32 + tl.arange(0, 32)
    start = tl.load(OFF + b)
    stop = tl.load(OFF + b + 1)
    length = stop - start
    tt = tl.arange(0, 128)
    acc = tl.full((32,), 0, tl.float32)
    for block in range(tl.cdiv(length, 128)):
        t = start + block * 128 + tt
        mask = (t[:, None] < stop) & (c[None, :] < C)
        x = tl.load(X + t[:, None] * C + c[None, :], mask, 0)
        if MODE == 2:
            w = tl.load(W + t[:, None] * C + c[None, :], mask, 0)
        elif MODE == 1:
            w = 1.0 / length
        else:
            w = 1.0
        acc = acc + tl.sum(x * w, axis=0)
    if MODE == 0:
        acc = acc / length
    tl.store(MEAN + b * C + c, acc, c < C)
    if MODE != 0:
        var = tl.full((32,), 0, tl.float32)
        for block in range(tl.cdiv(length, 128)):
            t = start + block * 128 + tt
            mask = (t[:, None] < stop) & (c[None, :] < C)
            x = tl.load(X + t[:, None] * C + c[None, :], mask, 0)
            if MODE == 2:
                w = tl.load(W + t[:, None] * C + c[None, :], mask, 0)
            else:
                w = 1.0 / length
            delta = x - acc[None, :]
            term = (delta * delta) * w
            var = var + tl.sum(tl.where(mask, term, 0), axis=0)
        tl.store(STD + b * C + c, tl.sqrt(tl.maximum(var, 1e-12)), c < C)


@triton.jit
def softmax_kernel(X, Y, OFF, C: tl.constexpr):
    b = tl.program_id(0)
    c = tl.program_id(1) * 32 + tl.arange(0, 32)
    start = tl.load(OFF + b)
    stop = tl.load(OFF + b + 1)
    length = stop - start
    tt = tl.arange(0, 128)
    maximum = tl.full((32,), float("-inf"), tl.float32)
    for block in range(tl.cdiv(length, 128)):
        t = start + block * 128 + tt
        mask = (t[:, None] < stop) & (c[None, :] < C)
        x = tl.load(X + t[:, None] * C + c[None, :], mask, float("-inf"))
        maximum = tl.maximum(maximum, tl.max(x, axis=0))
    total = tl.full((32,), 0, tl.float32)
    for block in range(tl.cdiv(length, 128)):
        t = start + block * 128 + tt
        mask = (t[:, None] < stop) & (c[None, :] < C)
        x = tl.load(X + t[:, None] * C + c[None, :], mask, float("-inf"))
        total = total + tl.sum(tl.exp(x - maximum[None, :]), axis=0)
    for block in range(tl.cdiv(length, 128)):
        t = start + block * 128 + tt
        mask = (t[:, None] < stop) & (c[None, :] < C)
        x = tl.load(X + t[:, None] * C + c[None, :], mask, float("-inf"))
        y = tl.exp(x - maximum[None, :]) / total[None, :]
        tl.store(Y + t[:, None] * C + c[None, :], y, mask)


class Packed:
    def __init__(self, model):
        self.model = model
        self.weights = {
            id(m): (m.weight.detach().permute(2, 1, 0).contiguous(), m.bias.detach())
            for m in model.modules()
            if isinstance(m, torch.nn.Conv1d)
        }

    def conv(self, layer, x, offsets, batch, relu=False):
        if layer is self.model.fc:
            return layer(x.unsqueeze(-1)).squeeze(-1)
        x = x.contiguous()
        weight, bias = self.weights[id(layer)]
        m, ci = x.shape
        co = layer.out_channels
        k = layer.kernel_size[0]
        d = layer.dilation[0]
        p = d * (k - 1) // 2
        out = torch.empty((m, co), device=x.device, dtype=x.dtype)
        conv[(triton.cdiv(m, 32), triton.cdiv(co, 64))](
            x,
            weight,
            bias,
            out,
            offsets,
            m,
            batch,
            ci,
            co,
            k,
            d,
            p,
            relu,
            num_warps=4,
            num_stages=2,
            enable_fp_fusion=False,
        )
        return out

    def stats(self, x, offsets, batch, mode, weight=None):
        c = x.shape[1]
        mean = torch.empty((batch, c), device=x.device, dtype=x.dtype)
        std = torch.empty_like(mean)
        stats_kernel[(batch, triton.cdiv(c, 32))](
            x,
            weight if weight is not None else x,
            offsets,
            mean,
            std,
            c,
            mode,
            num_warps=4,
            enable_fp_fusion=False,
        )
        return mean, std

    @torch.inference_mode()
    def __call__(self, items):
        lengths = [len(x) for x in items]
        batch = len(items)
        assert 0 < batch <= 64 and min(lengths) >= 5
        x = torch.cat(items, dim=0)
        offsets = torch.tensor(
            [0] + list(__import__("itertools").accumulate(lengths)),
            device=x.device,
            dtype=torch.int32,
        )
        ids = torch.repeat_interleave(
            torch.arange(batch, device=x.device),
            torch.tensor(lengths, device=x.device),
            output_size=len(x),
        )
        features = []
        for layer in self.model.blocks:
            if hasattr(layer, "res2net_block"):
                residual = x
                x = self.conv(layer.tdnn1.conv, x, offsets, batch, True)
                parts = x.chunk(layer.res2net_block.scale, dim=1)
                outputs = []
                for i, part in enumerate(parts):
                    if i == 0:
                        output = part
                    elif i == 1:
                        output = self.conv(
                            layer.res2net_block.blocks[i - 1].conv, part, offsets, batch, True
                        )
                    else:
                        output = self.conv(
                            layer.res2net_block.blocks[i - 1].conv,
                            part + output,
                            offsets,
                            batch,
                            True,
                        )
                    outputs.append(output)
                x = self.conv(layer.tdnn2.conv, torch.cat(outputs, dim=1), offsets, batch, True)
                mean, _ = self.stats(x, offsets, batch, 0)
                scale = self.conv(layer.se_block.conv1, mean, offsets, batch, True)
                scale = self.conv(layer.se_block.conv2, scale, offsets, batch).sigmoid()
                x = x * scale.index_select(0, ids) + residual
            else:
                x = self.conv(layer.conv, x, offsets, batch, True)
            features.append(x)
        x = self.conv(self.model.mfa.conv, torch.cat(features[1:], dim=1), offsets, batch, True)
        mean, std = self.stats(x, offsets, batch, 1)
        attention = torch.cat([x, mean.index_select(0, ids), std.index_select(0, ids)], dim=1)
        attention = self.conv(self.model.asp.tdnn.conv, attention, offsets, batch, True).tanh()
        attention = self.conv(self.model.asp.conv, attention, offsets, batch)
        weight = torch.empty_like(attention)
        c = x.shape[1]
        softmax_kernel[(batch, triton.cdiv(c, 32))](
            attention, weight, offsets, c, num_warps=4, enable_fp_fusion=False
        )
        mean, std = self.stats(x, offsets, batch, 2, weight)
        return self.conv(self.model.fc, torch.cat([mean, std], dim=1), offsets, batch)
