# Contract v0.1：C 全量启动后的复核

**复核补充（2026-09-29）：** 本文的文档一致性与回归结果不能替代来源区间和 codec 独立参考验收。已登记 [未关闭问题](codec-open-issues.md)；原“这些边界不阻止当前提取”的表述未覆盖这两项缺口。

复核范围：CONTRACT、12 份规范、Arrow 类型、全部示例，以及 selection/codec 的实际写入和恢复路径。
规范源仍为 docs/data-contract；统一根目录只同步约定，不放运行日志、待办或实验报告。
本次不改基础 27 列、已有 sample_id、已发布 selection 或 C 的数值 profile。

## 已修正

| 问题 | 处理 |
| --- | --- |
| feature 目录多一层难读的 profile 哈希 | 统一为 features/kind/run_id；profile_id 仍保存在 manifest/行和指纹中 |
| 01/07/10/12 对 build 的基础表表述不同 | 首版 build 从固定 codec 表派生，只增加 speaker 定位/就绪列；不再复制 codec_row |
| 多 codec run 和缺 codec 目标的覆盖不清 | binding_slot + codec 逻辑行偏移；重叠目标唯一裁决，缺失目标单独完整对账 |
| 09 的读取示例忽略 selection 文本/语言覆盖 | 投影覆盖列并按 null 回退，不按空字符串 truthiness 回退 |
| codec 发布尚未说明自包含文本 | 明确 9 个文本/来源字段、revision、逐 ID 对齐及物化回读摘要；类型和示例同步 |
| 提取验收与训练吞吐混在一起 | codec 提取验收和 build/训练验收分别列明；失败停留 incomplete 合法 |
| 通用 manifest 字段对无自身表的 build/selection 不适用 | 改为按布局适用，在 bindings/outputs 记录真正的表快照 |
| feature 示例的外部引用省略 branch | 新示例显式 main=null，检查器验证；历史缺失 branch 仍按 main 兼容 |
| annotation 示例少逐列 profile/schema | 补完整定义/摘要，检查器核对 Arrow 类型和质量值回读 |
| annotation 修订后再 strip 可能算错 text_revision | 规范要求固定修订原文；当前执行器未实现这条路径，因此明确拒绝非零 text_source |
| 在线 speaker 与离线特征字段混淆 | 在线路径不要求离线 profile/key；冻结热路径和在线读音频分别说明 |

当前 C selection 仅使用基础文本及规范化（source=0），不引用 annotation，因此不受最后这项限制影响。
运行使用固定 runtime-readable-v1，仓库后续增加的拒绝检查不会热更新在跑 worker；恢复使用 docs/codec.md 的固定入口。

## 规范精简

长期保留：身份、字段、指纹、快照、状态/缺失、覆盖、数值一致性、训练引用与清理保护。
C 的具体 batch/精度/缓存顺序和实测性能移至 [数值验证](codec-inference-validation.md)，生产 profile 保存完整定义。
各来源精确拒收细节移至 [来源排除协议](../source-exclusion-protocols.md)，08 只保留公共接入约束。
模型模板明确标记 example_only/runnable=false；FP32 示例不代表当前采用 FP32 生产路径。

## 实现边界

- 基础转换、首版 selection 与 C codec 已实现；C 运行状态以 active.json、status.json 和发布 manifest 为准。
- annotation worker/文本修订原文绑定、view 执行器、build 绑定器、训练读取器和通用加权 sampler 尚未完成。
- speaker 只有初探，异长补零混批不通过，未全量启动；完整 TTS loss/音色和吞吐验收仍需完成。
- 依赖保护规则已写入 contract；自动全局依赖回收器未实现，不能因此对生产快照进行自动清理。
- 当前 codec 输入为严格去重的 selection；通用重复目标复用和跨 profile 等价迁移是协议，不代表现有入口支持。

这些边界不阻止已批准的当前 codec 提取，也不代表训练已经就绪。

## 清理与验证

保留 tests 的转换/身份/存储/排除/selection/contract 回归，并补回当前 codec 的波形、批形状、
FP32 缓存、检查点损坏、发布中断恢复及文本对齐测试。只移除退役实验代码和可重建二进制产物。
源码调用检查未发现新的不可达模块；preview/inventory 是现用 CLI，codec_fast 被 C 调用，保留。
release_dataset 是固定快照读取接口，equivalent_payloads 是已测试的数值比较工具，也保留。

路径迁移核对 310 个已完成检查点、1,072,750 条结果，保留数值 profile 并成功续跑。
另清理 224,059,493 bytes 的实验音频、fixture、speaker 张量、旧 runtime 与冗余日志，
18 份生产验收 evidence 均保持原 SHA256；清单在 artifacts/codec-validation/c-fp16-fa2/experimental-cleanup.json。
之前 A/B 清理另见同目录 retired-ab-cleanup.json，不把当前活动检查点、模型或基础数据当缓存删除。

190 项完整回归通过；新增文本来源拒绝检查与 contract 专项共 6 项通过。Ruff 通过，
34 份 contract 类型/示例检查通过，部署至 unified 的副本逐文件字节一致。
CPU 回归不替代 GPU 验收；C 的八卡/重排/批组合/试听验证证据见数值验证记录。
