# Contract 复核：2026-10-09

范围：data-contract 总约定、全部 specs、类型描述和示例，以及 selection、text、merge、
codec/speaker profile、Emilia2 发布实现。此次检查不等于重扫全部已发布数据。

## 已澄清的规范

- 训练样本裁剪归 selection；reference speaker 是已实现的显式例外，保持原 sample 身份和完整
  text/codec，只裁取条件音频。04/06/12 补齐与 11 的交叉说明。
- 基础音频原字节保留规则补充 Emilia2 上游 short 物化的已有例外，避免总约定与 02/04 冲突。
- 一对一 annotation 的 sample_branch 是正式默认方式，独立 result_table 是显式选择；
  annotation 实验稿采用独立表，但没有宣布生产布局迁移。
- result_table 的成功零事件与失败均由 targets 记账；全无事件时 results 仍保留固定 schema 的空表。
  分支位置对齐检查不再被写成独立事件表必须与 base 逐行对齐。

## 尚未实现或需要后续修复

| 事项 | 核对结果及影响 |
| --- | --- |
| Annotation 正式执行 | 目前没有通用生产执行器；实验稿定义逻辑字段，尚未冻结任务 Arrow schema、模型 profile 和验收阈值。接口小测不等于 complete 发布。 |
| Selection 消费 annotation | `selection.py` 仍只实现 first-root，拒绝 annotation 输入；后续选择需实现排除继承、文本修订来源及指纹验证。 |
| 修订文本与现有 codec 复用 | `text/metadata.py` 拒绝 annotation 来源；`merge_features.py:merge_tables` 要求 codec 的九个兼容文本元数据列与 text 相等。纯音频 codes 可以复用，但采用新文本后不能直接拼旧表，需新元数据绑定与验收。 |
| Restoration | Sidon 派生音频发布、selection 选用及新音频身份的下游绑定未实现；不能把恢复路径当成已有音频的一个无影响分数。 |
| Emilia2 发布审计 | 专用 `ingest/emilia2/publication.py` 未完整输出 10 章通用 manifest 字段，例如 finished_at、code_sha256、excluded_source_records、checkpoint_code_versions/code_migration/finalization。证据文件虽复制到发布目录，manifest 未完整固定其引用和摘要。其 rejected_rows 统计失败输出及去重后的无效/冲突 ID，与普通 adapter 的输入记录计数口径不同，需明确区分候选数与唯一 ID 数。当前不修改运行中的任务或既有发布。 |

Emilia2 项应在其发布验收中补齐或明确专用协议；不能用历史读取器的 null 缺省声称新发布已符合要求。
上述缺口不表示现有三类 feature 数值错误，也不要求重算既有全量结果。

## 保留的边界

- 所有关联仍使用固定 dataset/release/target 身份、音频摘要和快照；物理行序只是经验证的优化。
- codec 的 exact_integer 是整数结果比较方式，不等于官方逐条推理逐 token 等同承诺。
  当前 profile 明确 `official_bitwise_equivalence=false`；speaker 数值容差与存储摘要精确校验分别保留。
- Reference merged 显式统计失败过滤；whole-sample merged 的 20 ms 容差不用于 reference 定位。
- 训练 token 化、loss mask、采样配方仍由训练端负责；不恢复 builds、training_plans、assets 或 views 发布层。
- 原三个独立特征表及旧 merged 保留，不因新版本产生而删除。

## 检查方法

运行 `scripts/check_contract.py` 检查本地链接、机器 Arrow 类型和示例一致性；
同步后用 `--target /workspace/data/DATA-TTS-UNIFIED` 逐文件验证部署副本。
该工具未校验所有业务语义、模型效果或全量生产数据；以上实现缺口由代码阅读确认。
