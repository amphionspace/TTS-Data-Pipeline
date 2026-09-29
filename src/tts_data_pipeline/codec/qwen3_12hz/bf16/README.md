# Packed BF16 implementation

`../encoder.py` loads the official BF16 tokenizer and calls `PackedModel`.
Production uses composition, with no experimental candidate inheritance chain.
Retired FP16 code and experimental snapshots remain in artifacts only.

| Module | Responsibility |
| --- | --- |
| `model.py` | CNN → Transformer → downsample → RVQ; packed causal attention |
| `convolution.py` | Sample-local convolution boundaries and fixed kernel settings |
| `linear.py` | Fixed single-accumulator GEMM and fused scale/residual epilogues |
| `quantization.py` | BF16 residual chains, norm reduction and near-boundary rechecks |

There are no length-policy tables, generated JSON files, shape profiling or
unknown-shape logging in production. Each operator uses fixed reduction settings;
input length determines tensor bounds and grid size, not a profiled strategy.
Model-intrinsic causal/stride padding remains; external waveform/sequence/tail
padding is absent. The existing 120-second model input limit still applies.

The algorithmic formulas match the original model. Floating-point addition is
not associative: different CUDA kernels and reduction orders can change rounding,
then discrete nearest-codebook selection and subsequent residual stages. We do
not promise bitwise equality with official single-sample inference. On 509 fixtures,
this fixed-reduction BF16 implementation differs in 119,477 / 1,140,240 codes
(10.48%); this is a token agreement measure, not a perceptual quality percentage.
An earlier length-aligned experimental version matched the official reference but
its policy machinery was removed at the user's request.

Tested environment: A800, PyTorch 2.8.0+cu126, cuDNN 91002, Transformers 4.57.3,
qwen-tts 0.1.1, Triton 3.4.0. The profile records runtime dependencies and all
numerical source hashes. The near-boundary RVQ guard is an empirical local
recheck and does not establish complete encoder equivalence.

See `docs/design-review/codec-bf16-experiments.md` for ablations and scope, and
`docs/design-review/codec-fp32-check.md` for the small FP32 comparison.
Full production launch is paused at the user's request; no new full run was started.
