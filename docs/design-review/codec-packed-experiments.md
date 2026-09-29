# 无外部补齐的 packed codec 后续实验

**参考标准纠正：** 本文此前称为“原生/独立参考”的结果，实际来自保留 C 数值设置的单条原长实现（FP16 encoder、全因果 FA2、FP32 码本缓存），不是未经修改的官方 tokenizer。本文百分比只表示与该 C 参考的差异，不能作为相对官方默认推理的准确度结论。官方未修改参考另存于 `artifacts/codec-padding-review/official-reference/`，正在重新对照。

2026-09-29。代码只在 `artifacts/codec-padding-review/packed-v1/` 和 `packed-v2/`；未接入生产 Encoder。
旧 unified codec 输出及状态已按用户要求删除，见 [清理记录与代码地图](../codec.md)。

## 参考与实现

[Thinking Machines 的 batch-invariant 研究](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/)
和[配套库](https://github.com/thinking-machines-lab/batch_invariant_ops)说明，控制归约方式能够避免批形状改变结果。
本实验据此编写本地固定 tile、固定 K 归约顺序的 Triton 矩阵乘法，没有安装或修改第三方库。
FP32 距离计算使用 IEEE 精度，不打开 TF32。

结合 [FlashAttention varlen 接口](https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/flash_attn_interface.py)，
将真实 token 打包，位置编号在每条音频从零开始，attention 用独立序列边界隔离。
投影、MLP 和 RVQ 在真实 token 上批量执行。RVQ 请求仍为 16 个码本，不能误用模型全部 32 个码本。
这是新数值实现；batch invariance 不等于与原生 cuDNN/cuBLAS 逐码字相同。

进一步实现 ragged CNN：卷积输出每一行定位所属音频，只读取该音频的有效范围；
保持原始 causal/stride/constant 边界和 downsample 的 replicate 边界。
不创建补齐音频、补齐 token 或 dummy sample。内部计算 tile 的掩码不向模型添加额外时间位置。

## 一致性结果

沿用 16 来源各两条真实音频加 13 条边界，共 45 条、123,584 个码字。
独立参考固定为此前无外部补齐、batch=1 的原生 encoder，避免随着候选修改参考。

| 候选 | 相对独立参考不同码字 | 单条分批 vs 一次性 packed | 每 7 条分批 | 逆序 |
| --- | ---: | ---: | ---: | ---: |
| 普通 packed matmul，原生逐条 CNN | 1,327 | 1,336 | 907 | 0 |
| 固定归约 packed matmul，原生逐条 CNN | 1,572 | 0 | 0 | 0 |
| 固定归约 + ragged CNN（v1） | 17,903 | 0 | 0 | 0 |

第三种候选在 GPU 1/2 的全部 45 条 codes 也完全一致。结果仅覆盖这些样本和硬件，不能外推所有 GPU 架构。

单独以 FP64 卷积为参考，检查每个 MimiConv1d 在长度 1/7/16/33 的随机输入上的输出，
包含 causal stride 和 replicate 边界。`convolution-reference.json` 保存候选和原生的误差。
观察到部分层的候选误差更小，但没有据此断言最终 codes 更准确；多层浮点变化仍会被 RVQ 放大。

## 性能

共享 A800 GPU 2，同一轮依次测 canonical 与候选；预热后 3 次中位数，编码调用含 CPU 打包、上传和下载，
不含原始文件读取、解码、写 Lance。不同轮次的 GPU 负载可变。

| 实现 | 满短批 64 条 | 32 条真实混长 |
| --- | ---: | ---: |
| 固定归约 packed，原生 CNN | 0.1285 s | 0.1793 s |
| 该轮旧 canonical | 0.0656 s | 1.0863 s |
| 固定归约 packed + ragged CNN v1 | 0.0689 s | 0.2238 s |
| v1 该轮旧 canonical | 0.0662 s | 1.0843 s |

诊断用同步 hooks 显示原生逐条 CNN 是主要调度开销；该 hooks 记录不能当成正常吞吐数据。
ragged CNN 改善满短批，但在此混长样本上慢于保留原生 CNN 的 packed 路径。
因此要同时考察饱和短批、长音频、真实长度分布与 CPU/IO 流水线，不能只选最有利场景。

v2 进一步把行数改为运行时参数，降低按精确长度反复 JIT 编译的成本，并尝试较大的固定 tile。
固定 64 行 tile 的 v2 同卡 7 次中位数：满短批 **0.0466 s**，旧 canonical **0.0660 s**（约 1.42 倍吞吐）；
32 条混长 **0.2786 s**，旧 canonical **1.0838 s**（约 3.89 倍吞吐）。v2 混长慢于 v1，不能笼统称为全面加速。
45 条单条/7 条分批/逆序均一致，v2 GPU 2 与 v1 两卡的全部码字也一致。
首次全 45 条调用含编译约 6.02 s；编译冷启动必须单独计入。具体结果在 `packed-v2/results-rows64-gpu2.json`。
这条路线不为每条音频长度缓存完整 CUDA Graph，避免上一轮那类 Graph 形状缓存成本，但仍有 Triton 编译缓存。

## 重建检查

16 个来源各选一条真实音频，保存 source/native/packed/ragged_cnn 四组 WAV。
路径：`packed-v1/listening/`，统计：`packed-v1/quality.json`。
相对于原生独立参考，候选重建相对原始音频的指标变化如下：

| 候选 | 波形 SNR 平均变化（越高越好） | 多分辨率 log 频谱 RMSE 平均变化（越低越好） |
| --- | ---: | ---: |
| 固定归约 packed，原生 CNN | -0.0056 dB | -0.0039 dB |
| 固定归约 packed + ragged CNN | +0.0560 dB | -0.0105 dB |

ragged CNN 的单样本 SNR 变化范围为 -0.3841 至 +0.6375 dB，频谱 RMSE 变化范围为 -0.0685 至 +0.0463 dB。
指标采用直接对齐的 waveform SNR 和 512/1024/2048 FFT 的 log 幅度 RMSE；这些是失真诊断，
不是感知质量或内容准确率指标。没有完成人工试听验收，也没有完成全库质量结论。

## 下一步条件

当前实验表明，去掉外部 padding 后仍有接近旧满批速度的空间，而且能在测试范围内消除换批次造成的漂移。
生产切换前需要扩大真实数据覆盖，增加不同总 token 数、邻居替换、批位置、重启和 GPU 检查，
进行重建试听与内容质量验收，并测有内存预算的端到端吞吐。若必须与当前原生单条逐码字完全一致，
这条 packed 新内核路线尚不满足，原长 Graph 候选仍是已有的等价参考路线。

退出阶段的 `fatal library error, lookup self` 在未使用 Graph 的 FP64 对照进程中也出现，退出码仍为 0。
已不能只归因于 Graph 缓存；尚未定位，不能宣称资源销毁稳定性已验收。

汇总与实验源码/结果摘要：`artifacts/codec-padding-review/packed-summary.json`。

进一步的精度归因与不降速改进见 [精度实验](codec-precision-experiments.md)。
