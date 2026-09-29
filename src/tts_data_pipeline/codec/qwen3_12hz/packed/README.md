# Packed BF16 / FP32

`../encoder.py` loads the official tokenizer in the requested dtype. FP32 is the
production default; `precision="bf16"` retains the previous BF16 computation.
Both paths share these four modules rather than duplicate the model:

| Module | Responsibility |
| --- | --- |
| `model.py` | CNN → Transformer → downsample → RVQ; sample-local causal attention |
| `convolution.py` | Sample boundaries, fixed reductions, cached FP32 weight layout |
| `linear.py` | GEMM and fused dtype-preserving scale/residual epilogues |
| `quantization.py` | Residual chains, native norm order and near-boundary rechecks |

FP32 keeps weights, activations, accumulators and residuals in float32. Triton
matrix products use `tf32x3`: three compensated Tensor Core products. This is an
approximation to FP32 multiplication, not ordinary TF32 and not a guarantee of
strict IEEE FP32 arithmetic. Native PyTorch matmul and cuDNN TF32 are disabled
in this path. Contiguous convolution weights are cached once in the existing
reduction order. BF16 keeps its original layout, Tensor Core products and casts.

No length-policy tables, generated JSON, shape profiling or unknown-shape scans
are needed. Lengths determine bounds and grid sizes. Model-intrinsic causal,
stride and replicate padding remains; external waveform/batch padding is absent.
The existing 120-second input and 64-sample/480-second batch limits remain.

Floating-point reduction order can change discrete nearest-codebook decisions.
Neither path promises bitwise equality to official single-sample inference; the
reference must use the same model dtype and actual unpadded waveform. The RVQ
near-boundary recheck is empirical and does not prove whole-model equivalence.
Profiles record dtype, multiplication mode, all numerical source hashes, model
weights and runtime dependencies. BF16 and FP32 outputs have separate identities.

See `docs/design-review/codec-fp32-optimization.md` for acceptance evidence and
`docs/design-review/codec-bf16-experiments.md` for historical BF16 experiments.
