"""Fixed-reduction matrix multiplication and fused BF16 scale/residual epilogues."""

import torch
import triton
import triton.language as tl


@triton.jit
def mm_kernel(
    A,
    B,
    C,
    M,
    N: tl.constexpr,
    K: tl.constexpr,
    AM: tl.constexpr,
    AK: tl.constexpr,
    BK: tl.constexpr,
    BN: tl.constexpr,
):
    rows = tl.program_id(0) * 32 + tl.arange(0, 32)
    cols = tl.program_id(1) * 64 + tl.arange(0, 64)
    ks = tl.arange(0, 32)
    acc = tl.full((32, 64), 0, tl.float32)
    for start in range(tl.cdiv(K, 32)):
        k = start * 32 + ks
        a = tl.load(
            A + rows[:, None] * AM + k[None, :] * AK, (rows[:, None] < M) & (k[None, :] < K), 0
        )
        b = tl.load(
            B + k[:, None] * BK + cols[None, :] * BN, (k[:, None] < K) & (cols[None, :] < N), 0
        )
        acc = tl.dot(a, b, acc, input_precision="ieee")
    tl.store(C + rows[:, None] * N + cols[None, :], acc, (rows[:, None] < M) & (cols[None, :] < N))


@triton.jit(do_not_specialize=["M"])
def linear_kernel(
    A,
    W,
    Y,
    M,
    N: tl.constexpr,
    K: tl.constexpr,
    AM: tl.constexpr,
    AK: tl.constexpr,
    WK: tl.constexpr,
    WN: tl.constexpr,
    ROWS: tl.constexpr,
    SCALE,
    RES,
    FUSED: tl.constexpr,
):
    rows = tl.program_id(0) * ROWS + tl.arange(0, ROWS)
    cols = tl.program_id(1) * 64 + tl.arange(0, 64)
    ks = tl.arange(0, 32)
    acc = tl.full((ROWS, 64), 0, tl.float32)
    for step in range(tl.cdiv(K, 32)):
        k = step * 32 + ks
        a = tl.load(
            A + rows[:, None] * AM + k[None, :] * AK, (rows[:, None] < M) & (k[None, :] < K), 0
        )
        w = tl.load(
            W + k[:, None] * WK + cols[None, :] * WN, (k[:, None] < K) & (cols[None, :] < N), 0
        )
        acc = tl.dot(a, w, acc, input_precision="ieee")
    if FUSED:
        scale = tl.load(SCALE + cols, cols < N, 0).to(tl.float32)
        # Preserve the model's BF16 cast after GEMM and after layer scaling.
        scaled = acc.to(Y.dtype.element_ty).to(tl.float32) * scale[None, :]
        scaled = scaled.to(Y.dtype.element_ty).to(tl.float32)
        residual = tl.load(
            RES + rows[:, None] * N + cols[None, :], (rows[:, None] < M) & (cols[None, :] < N), 0
        ).to(tl.float32)
        acc = scaled + residual
    tl.store(Y + rows[:, None] * N + cols[None, :], acc, (rows[:, None] < M) & (cols[None, :] < N))


def matmul(x, weight, scale=None, residual=None):
    """Rows are independent: batch/sequence lengths never choose a reduction strategy."""
    m, k = x.shape
    n = weight.shape[1]
    rows = 64 if k <= 256 else 32
    output = torch.empty((m, n), device=x.device, dtype=x.dtype)
    linear_kernel[(triton.cdiv(m, rows), triton.cdiv(n, 64))](
        x,
        weight,
        output,
        m,
        n,
        k,
        *x.stride(),
        *weight.stride(),
        rows,
        scale,
        residual,
        scale is not None,
        num_warps=4,
        num_stages=2,
        enable_fp_fusion=False,
    )
    return output
