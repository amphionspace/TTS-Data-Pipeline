"""Packed native STFT windows: concatenate only actual frames, with intrinsic reflection."""

from functools import lru_cache

import torch
import triton
import triton.language as tl
from librosa.filters import mel as mel_filter


@lru_cache(maxsize=1)
def filters():
    basis = torch.from_numpy(
        mel_filter(sr=24000, n_fft=1024, n_mels=128, fmin=0, fmax=12000)
    ).float()
    return basis, torch.hann_window(1024, dtype=torch.float32)


@triton.jit(do_not_specialize=["M", "B"])
def windows_kernel(WAVE, WINDOW, SO, FO, OUT, M, B):
    r = tl.program_id(0)
    k = tl.arange(0, 1024)
    lo = 0
    hi = B
    for _ in range(7):
        mid = (lo + hi) // 2
        end = tl.load(FO + mid)
        right = end <= r
        lo = tl.where(right, mid + 1, lo)
        hi = tl.where(right, hi, mid)
    sample = tl.minimum(lo - 1, B - 1)
    start = tl.load(SO + sample)
    stop = tl.load(SO + sample + 1)
    length = stop - start
    local = r - tl.load(FO + sample)
    t = local * 256 - 384 + k
    t = tl.where(t < 0, -t, t)
    t = tl.where(t >= length, 2 * length - 2 - t, t)
    x = tl.load(WAVE + start + t)
    w = tl.load(WINDOW + k)
    tl.store(OUT + r * 1024 + k, x * w)


class GPUMel:
    def __init__(self, device):
        basis, window = filters()
        self.basis = basis.to(device)
        self.window = window.to(device)
        self.device = device

    @torch.inference_mode()
    def __call__(self, waves):
        lengths = [len(w) for w in waves]
        frames = [n // 256 for n in lengths]
        assert min(frames) >= 5 and len(waves) <= 64
        from itertools import accumulate

        so = torch.tensor([0] + list(accumulate(lengths)), device=self.device, dtype=torch.int32)
        fo = torch.tensor([0] + list(accumulate(frames)), device=self.device, dtype=torch.int32)
        wave = torch.cat(waves).to(self.device)
        total = sum(frames)
        windowed = torch.empty((total, 1024), device=self.device, dtype=torch.float32)
        windows_kernel[(total,)](
            wave,
            self.window,
            so,
            fo,
            windowed,
            total,
            len(waves),
            num_warps=4,
            enable_fp_fusion=False,
        )
        spectrum = torch.fft.rfft(windowed, dim=-1)
        magnitude = torch.sqrt(torch.view_as_real(spectrum).pow(2).sum(-1) + 1e-9)
        result = torch.log(torch.clamp(self.basis @ magnitude.T, min=1e-5)).T.contiguous()
        return list(result.split(frames, dim=0))
