# 核查与待办

当前方案为 Lance v0.1，共 16 个 adapter；截至 2026-09-29，16 个来源均按各自输入/排除范围完成发布，Emilia2 暂不接入。
规范见 [data-contract](../data-contract/README.md)，21 个来源的接入映射见 [datasets](../datasets.md)。

| 文档 | 用途 |
| --- | --- |
| [Codec FP32 小测](codec-fp32-check.md) | 同精度官方参考、固定计算、耗时与显存；全量暂停 |
| [Codec BF16 验收与代码整理](codec-bf16-experiments.md) | 官方 BF16 单条参考、实际满载、规则消融与生产实现 |
| [Codec padding 实验与研究](codec-padding-experiments.md) | 独立参考、无补齐/变长/Graph 候选、上游实践和性能边界 |
| [codec-packed-experiments.md](codec-packed-experiments.md) | 固定归约、真实变长卷积、性能与重建对照 |
| [Codec 精度归因与等速优化](codec-precision-experiments.md) | 舍入/累加差异、阶段替换、172 条验证与同卡性能门槛 |
| [Codec 历史问题与修复进展](codec-open-issues.md) | 区间核查、padding 归因和后续验收范围 |
| [本轮 contract 复核](contract-review.md) | C 目录、文本物化、build 引用、规范精简、清理和实现边界 |
| [验收与契约复核](adapter-contract-review.md) | 原四个来源与新增九个来源的验证范围、契约修正和性能边界 |
| [LibriHeavy / MLS 核查](libriheavy-mls-check.md) | 配置交集、来源清单、MLS 坏包与明确排除 |
| [Emilia / YODAS 本地核查](emilia-local-check.md) | 4,343 包首条检查、采样解码、字段映射与训练注意事项 |
| [Codec / speaker 约定核查](codec-speaker-contract-review.md) | 冻结/解冻两条路径、前处理版本、与训练仓库的接口差距 |
| [Codec / speaker 提取计划](feature-extraction-plan.md) | 独立环境、空音频/文本过滤、可读命名、8 卡调度与验收阶段 |
| [Codec 实测与训练接口](codec-inference-validation.md) | 16 码本来源、模型初始化差异、真实 backend、数值一致性与 8 卡吞吐 |
| [特征输入核查](feature-input-review.md) | 全量语言条数/时长占比、缺失标签与逐数据集裁剪初查 |
| [已发布数据清理核验](published-cleanup-review.md) | 已清理临时文件、MLS SQLite 的数量、核验和证据摘要 |
| [Selection 分支验证](selection-validation.md) | LJSpeech完整音频、Emilia全部行数增列性能、清理与定位回归 |
| [数据问题与处置](data-selection-review.md) | 重复/冲突、坏音频、缺失标签、首尾空白证据与后续实施边界 |
| [当前问题](issues.md) | 待实现的标注、视图、codec、训练构建及其他来源问题 |

reports 仅保留来源核查、验收和排除依据，已忽略 Git。
早期格式选型草案、阶段日志、旧目录盘点快照和一次性探查脚本已清理。
真实样本预览与完整转换通过不代表音文一致性、音色身份和训练吞吐已验证。
