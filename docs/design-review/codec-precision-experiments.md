# Codec 精度归因与等速优化

**参考标准纠正：** 本文此前称为“原生/独立参考”的结果，实际来自保留 C 数值设置的单条原长实现（FP16 encoder、全因果 FA2、FP32 码本缓存），不是未经修改的官方 tokenizer。本文百分比只表示与该 C 参考的差异，不能作为相对官方默认推理的准确度结论。官方未修改参考另存于 `artifacts/codec-padding-review/official-reference/`。后续已改以官方 BF16 为主标准，见 [BF16 与真实负载验证](codec-bf16-experiments.md)。

2026-09-29。目标：保留无外部 padding 和批次独立性，在不降低此前最快 packed-v2 吞吐的条件下，
减少相对独立原长、batch=1 原生参考的差异。该参考仍是 FP16 模型路径，不把它等同于无限精度真值。
全部实现与数据在 `artifacts/codec-padding-review/precision/`；未接入生产、未向 unified 发布。

## 已定位的主要原因

首先对 45 个固定样本做四阶段替换，未替换的阶段均用原生实现。

| 只替换哪个阶段 | 不同码字 / 123,584 |
| --- | ---: |
| 全原生（诊断控制组） | 0 |
| 原始实验 CNN | 18,036 |
| packed Transformer | 1,589 |
| packed downsample | 80 |
| packed RVQ | 17 |

这些数量不可相加：上游浮点改变后，RVQ 的决策和后续残差都可能改变。
完整旧候选是 17,903 个差异，其中第 1 码本为 67 个，第 16 码本为 2,245 个；差异明显向后续声学残差码本累积。

最主要的可修复偏差是 **卷积 bias 的舍入边界**。
原生 PyTorch cuDNN 路径先得到卷积输出，再独立加 bias；旧 Triton 原型把 bias 加到 FP32 累加结果后才转 FP16。
两种写法数学等价，但 FP16 舍入位置不同。
对齐为“累加 → FP16 → 加 bias → FP16”后，完整候选的差异降至 3,703，单独 CNN 替换降至 3,695。
[PyTorch 卷积源码](https://github.com/pytorch/pytorch/blob/v2.8.0/aten/src/ATen/native/Convolution.cpp)

首个单声道 7 点卷积还有另一类差异。相同输入下，两个真实样本的 Tensor Core 实现分别有 190/7,627,904、
1,211/45,723,840 个输出元素不同；改为正向 FP32 FMA 后均为零。反向 FMA 和 FP64 没有复现原生结果。
因此“提高算术精度”和“复现原生浮点结果”是两个目标；没有通过盲目升精度来降低差异。

原生 kernel profiling 还显示 CNN 混用了 cuDNN SCUDNN、XMMA、CUTLASS 以及 GEMM 路径。
CUTLASS 的优化卷积迭代器先遍历 filter，再推进通道 tile；单纯交换整个 C/K 维度并不能完整复现这种归约。
[官方迭代器](https://github.com/NVIDIA/cutlass/blob/main/include/cutlass/conv/threadblock/conv2d_fprop_activation_tile_access_iterator_optimized.h)

## 已通过扩大样本验证的候选

`precision/candidate.py` 固定：bias 舍入对齐、首层 FP32 FMA、其余卷积 spatial-first 固定归约，
64 行/32 K tile、4 warps、2 stages，保留 packed varlen attention，并融合 RVQ 的距离后处理、argmin 与残差更新。
RVQ 融合关闭 FMA contraction、保持原有运算次序，对照未融合版的 codes 完全一致。

| 样本范围 | 旧 packed-v2 | 精度候选 |
| --- | ---: | ---: |
| 原 45 条 / 123,584 codes | 17,903（14.49%） | 3,495（2.83%） |
| 16 来源新增 127 条 / 232,496 codes | 32,486（13.97%） | 6,866（2.95%） |
| 合计 172 条 / 356,080 codes | 50,389（14.15%） | 10,361（2.91%） |

新增样本不参与早期参数筛选，排除原 32 条真实音频，按审计通过的 1–30 秒样本时长分层抽取；
各来源最多 8 条，一处符合条件的剩余证据仅 7 条，所以合计 127 而非 128。
它仍是现有审计样本中的有限集合，不能视为全库随机验收。
172 条的 GPU 1/2 对账、单条、每 7/8 条分批和逆序比较均为零差异。

## 速度门槛

GPU 2，共享 A800；同进程各预热 6 轮，交替顺序测 10 次，中位数如下。
对照是此前最快的 packed-v2，不是更慢的 canonical padded 版本。
包含 CPU 打包、上传/下载；不含文件读取、音频解码和写库。

| 负载 | packed-v2 | 精度候选 |
| --- | ---: | ---: |
| 64 条短音频 | 57.00 ms | 36.01 ms |
| 32 条真实混长 | 279.25 ms | 164.97 ms |
| 约 5 秒单条 | 13.43 ms | 11.84 ms |
| 120 秒单条 | 70.30 ms | 41.32 ms |

早期单独测修复版曾出现约 70 ms 的短批成绩，不能与另一轮 46 ms 直接比较。
充分预热、同卡交替测试以及最终组合候选显示没有观察到这些负载上的吞吐退化。
共享 GPU 测量不是生产 SLA，也不包括编译冷启动。

## 已排除或收益不足的方向

- 只改变普通 K tile 16/32/64/128：同一归约顺序下码字相同，不能解决精度问题；部分配置反而更慢。
- dense 单条 attention：在固定 CNN 上有时略减差异，但加入修正 CNN 后未改善整体，短批明显变慢。
- 同长分组 dense attention：不补齐，但分批后仍有 238 个不同码字，不能接受。
- 直接采用 PyTorch cdist 的 augmented matmul 运算顺序：当前整体候选码字未进一步变化。
  [PyTorch 距离源码](https://github.com/pytorch/pytorch/blob/v2.8.0/aten/src/ATen/native/Distance.cpp)
- 对最后两层 CNN/downsample 恢复逐条原生：测试组合没有胜过当前候选的整体差异，并增加调度成本。
- 单纯提高首层至 FP64：不匹配原生的 FP32 累加舍入，不能以“精度更高”冒充参考一致性。

## 证据与限制

`ablation.json`、`layer-trace.json`、`first-conv-results.json`、`native-cnn-kernels.json` 保存差异归因。
`ablation-invalid-noncontiguous.json` 是已作废的诊断：当时把非连续原生输出传给要求连续布局的实验卷积；
修正诊断输入后重跑的 `ablation.json` 才有效。实验主编码器始终使用连续打包张量，该诊断问题不影响此前整体码字计数。

`holdout-results.json`、`candidate-validation.json` 是扩大样本与速度门槛证据。
`speed-tuning.json`、`attention-results.json`、`native-tail-results.json` 保存各尝试，包括失败和退化路线。

剩余差异不等于等比例音质下降，但也不能仅以相对参考更近就宣称质量合格。
之前的 16 条试听对照属于旧候选，不能自动作为此候选的验收。
仍需对最终固定实现做更广质量与生产流水线验证；退出库警告尚未定位。
目前没有证据证明“所有可能的精度优化已穷尽”，正在进一步核对原生卷积的分组累加次序。
