# Codec 无外部 padding：实验、加速方向与代码整理

**参考标准纠正：** 本文此前称为“原生/独立参考”的结果，实际来自保留 C 数值设置的单条原长实现（FP16 encoder、全因果 FA2、FP32 码本缓存），不是未经修改的官方 tokenizer。本文百分比只表示与该 C 参考的差异，不能作为相对官方默认推理的准确度结论。官方未修改参考另存于 `artifacts/codec-padding-review/official-reference/`，正在重新对照。

2026-09-29。实验目录：`artifacts/codec-padding-review/`。旧 C 已退役，旧 unified codec 输出和状态已按用户要求删除；实验不向 unified 发布新 codec。
这里的“无 padding”指不补音频时间长度、不补 batch 空槽；模型固有的卷积边界处理仍然保留。

## 已确认的问题

固定权重、FP16 encoder、FP32 码本/量化距离、全因果 FA2，与直接按实际长度、batch=1 调用模型的独立参考比较。
覆盖 16 个数据集各 2 条真实音频，加 13 条长度边界，共 45 条、123,584 个码字。该规模用于发现问题，不代表全库验收。

| 调用方式 | 发生码字变化的样本 | 不同码字 |
| --- | ---: | ---: |
| 现有逐层实现，原长、batch=1 | 0/45 | 0 |
| 只补时间长度到 2 秒桶 | 19/45 | 558 |
| 完整 canonical 时间桶和 batch 空槽 | 40/45 | 2,013 |

因此，最后裁掉多余 codes 并不能恢复原长结果。形状改变引起的浮点计算差异也会通过 RVQ 放大；
不能把所有差异都解释为错误的尾部信息泄漏，更不能仅凭整数差异断言音质变差。
原长且相同 batch 形状时 CNN、Transformer、downsample 全部逐值一致，说明现有逐层边界处理本身并未在这些样本上偏离参考。

`Encoder.encode_single()` 仍走 canonical 路径，不能用它作为无补齐参考。
完整逐阶段、逐码本、逐时间位置证据在 `comparison.json`，输入身份在 `fixtures.json`。

## 已测试的方案

1. **原长逐条执行**：45 条换序、单独调用及 GPU 1/2 比较均一致；正确性基线清晰，但短音频满批吞吐不足。
2. **变长 attention**：真实 token 打包，用 `cu_seqlens` 隔离音频。测试中分批、换序、两卡均一致，
   但与 dense 单条参考有 805 个码字差异，且满短批仍慢。它是新数值路径，不能直接视为等价替换。
3. **原长 CUDA Graph**：保留每条音频的实际运算形状，减少 Python/kernel launch 开销。
   full 模式捕获完整 encoder；tail 模式只捕获 CNN 后的 Transformer/downsample/RVQ，按真实 latent 长度复用。
   两种模式在 45 条及逆序调用上均与单条参考零码字差异。
4. **多 stream 并发 Graph**：每个执行通道使用独立输入、输出和 graph 内存池。2/4/8 路测试均为零码字差异，
   8 路显著改善短音频满批，但首次捕获成本和缓存占用仍需优化。

共享 A800 80GB，热运行三次中位数，含上传/下载，不含文件 IO/音频解码。下表来自不同实验轮次/卡，
用于定位数量级；不能当作受控生产吞吐排名。后续同卡对照见 `concurrent-tail-graph.json`。

| 路径 | 64 条短音频满桶 | 32 条真实混长 |
| --- | ---: | ---: |
| 旧 canonical（GPU 1） | 0.0642 s | 1.0757 s |
| 原长逐条（GPU 1） | 0.7217 s | 0.3838 s |
| full Graph 顺序执行（GPU 1） | 0.1836 s | 0.1682 s |
| tail Graph 顺序执行（GPU 2） | 0.1786 s | 0.1720 s |
| full Graph 8 路并发（GPU 2） | 0.0743 s | 0.1470 s |

full Graph 的 64 条不同原长短音频首次调用约 10.7 秒，8 路实验最终 PyTorch reserved memory 约 19.7 GiB
（包含模型及此前本进程缓存，不能全归为 Graph 实际占用）。随机原始长度多时，热缓存成绩不代表实际运行速度。
tail Graph 可降低形状种类：64 条原长不同但 CNN 输出同长的音频，顺序实验只需捕获一次；
它仍然没有补音频或 token。需以真实长度分布评估缓存命中率、淘汰、冷启动与峰值内存。

同卡补充：tail Graph 的 8/16 路短音频满批均约 0.109 秒，旧 canonical 约 0.066–0.067 秒；
32 条混长约 0.148 秒，对照约 1.09 秒。8/16 路的 45 条及逆序码字均一致，增加并发没有继续改善。
该脚本完成结果写入、退出码为 0，但退出阶段日志有 `fatal library error, lookup self`；
原因尚未定位，需补充资源销毁/重复启动稳定性验证，不能把它列为生产稳定性通过。

## 查阅他人实现后可借鉴的做法

- Mimi 原作者在流式实现中使用 `CUDAGraphed` 包装 encoder/decoder 与 Transformer。
  这支持尝试 Graph 降低启动开销，但其流式状态和上下文不能直接替换当前全因果离线路径。
  [Mimi 实现](https://github.com/kyutai-labs/moshi/blob/main/moshi/moshi/models/compression.py)
- PyTorch 说明 Graph 对形状/执行结构有约束；共享内存池还要求遵守捕获/重放顺序及并发限制。
  当前实验采用独立池，避免为了省内存引入重放覆盖。
  [CUDA Graph 文档](https://docs.pytorch.org/docs/main/notes/cuda.html#cuda-graphs)
- FlashAttention 提供 varlen 接口，可用真实序列边界隔离 packed token；这解决 attention 的变长表示，
  不会自动解决 CNN 边界、线性层与 RVQ 的批形状数值差异。
  [FlashAttention 接口](https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/flash_attn_interface.py)
- vLLM 已提供 beta batch invariance：使用确定性内核，控制不同 batch 下的数值行为；其验证列表不包含这里的 Mimi codec。
  不能通过给本项目设置一个 vLLM 环境变量就获得同样保证。
  [vLLM 文档](https://github.com/vllm-project/vllm/blob/main/docs/features/batch_invariance.md)
- Thinking Machines 的研究解释了批形状改变归约顺序的问题，并讨论固定归约策略的实现。
  这为下一阶段 packed 线性层、norm 和 RVQ 内核提供了思路；新内核仍需独立数值/质量验收。
  [作者研究](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/)

建议近期继续推进**原长执行 + 按真实 latent 长度复用 Graph + 有界并发**，以独立参考逐码字一致作为门槛。
若真实长度分布导致缓存收益不足，再实现固定归约的 packed 计算；不能只换 varlen attention 就宣布完成。
生产接入前还需更大真实样本、跨卡/重启/缓存淘汰检查、重建质量与完整流水线吞吐验证。
当前没有最终生产修复或新的 acceptance。

## 代码整理与验证

将 `codec.py` 中音频处理、profile/身份、CPU 调度拆到 `codec_audio.py`、`codec_profile.py`、`codec_schedule.py`。
保留原公共导入接口；`codec_batch.py` 继续导出调度函数以兼容原调用；worker 直接引用纯 CPU 调度模块。
模型执行、批处理数值路径、卷积优化、任务发布和文本物化的职责见 [代码地图](../codec.md#代码职责)。

整理后的源码未启用 artifacts 中的实验候选。45 条 canonical GPU 输出在整理前后哈希完全相同；
profile 除源码摘要外全部字段一致。新增模块均纳入源码摘要，因此 profile_id 会变化，旧冻结任务不能静默换源码恢复。
全量 pytest **190 passed**；相关模块 Ruff 通过；34 份 data contract 检查通过。
证据：`refactor-before.json`、`refactor-after.json`、`refactor-full-tests.log`。

首轮 `summary.json` 保留当时源码摘要，后续结果单独记录，不能用整理后的源码摘要冒充首轮测试实现。

## Tokenizer 子包与旧产物清理

实现进一步集中到 `src/tts_data_pipeline/codec/qwen3_12hz/`，文件命名为 `encoder/audio/profile/schedule/batch/fast/run/text.py`。CLI、contract 检查与测试均已更新导入；顶层 codec 保留惰性兼容入口。目录迁移后全量 190 个测试通过，34 份 contract 检查通过。GPU 对账见 `refactor-package.json`。

用户随后明确要求删除 unified 中所有旧 codec features；已清理 16 个输出目录和 3 个 codec 状态目录，2,245 个文件、约 15.8 GB。清单见 `artifacts/codec-padding-review/unified-cleanup/`。此前章节和历史报告中的暂停/可恢复描述仅代表清理前状态。

后续 packed / batch-invariant / ragged CNN 实验见 [续篇](codec-packed-experiments.md)。
