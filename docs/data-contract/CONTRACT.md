# 统一数据契约 v0.1

## 1. 目标和处理顺序

```text
原始数据（只读）
    ↓ 每个来源一个 adapter
samples.lance：原音频 + 基础文字/语言/speaker/来源
    ├─ selection 分支 → 原行 + 入选主原因/多重原因，不复制音频
    ├─ annotation 平级分支 → 固定任务结果；一对多结果另存表
    └─ codec / speaker embedding / text → 独立 Lance 特征表
              ↓ 已发布 manifest、固定快照、样本身份与覆盖信息
训练仓库消费数据（训练专用构建与采样由训练端管理）
```

数据端负责原始接入、选择、派生特征和发布；训练端负责训练专用构建、token 化、配对和采样。
原始来源不被修改；已发布数据通过固定版本交付，不因训练实验修改源表。
冻结 speaker encoder 可直接读取缓存 embedding；未来解冻则读取原音频样本，按当前 frontend 在线提取，
不把某一种 mel 规定为统一层必备产物。参见 [Speaker 两条路径](specs/11-speaker-embeddings.md)。

## 2. 规范边界

本契约保存长期可互操作的字段、身份、引用、缺失/失败状态、覆盖和发布/保留约束。
具体模型/精度/数值路径属于已验证 profile；一次任务的来源范围、硬件并发和性能属于 run manifest。
实验过程、部署路径的当前运行 ID、代码修复历史与实现进度只记录在 pipeline 文档。
规范更新不改变已有 manifest/profile 哈希，不重写已发布数据；涉及字段/身份语义演进时按第 10 章处理。

## 3. 固定决定

1. 统一存储使用本地 Lance 表，不要求启动数据库服务。HF Parquet 可以作为输入或将来的交换导出格式。
2. 根目录为 `/workspace/data/DATA-TTS-UNIFIED`。按 dataset 管理发布，每个 release 的主表是 `samples.lance`。
3. 首次发布名为 `v0.1`。主表在物理上由 Lance 管理多个数据文件；运行 batch 不是数据目录或训练读取单元。
4. 独立音频文件保留原始编码 bytes、采样率和声道；不在基础接入时重采样或归一化。
   Emilia2 等按上游 short 从载体物化基础样本的专用接入例外，按 02/04 固定裁剪与编码规则。
5. 基础列按 27 列 schema 写入；缺失文本、语言、speaker 使用 null。后续标注不覆盖基础列。
6. 全部基础记录 `source_split=train`；上游 split/config/tier 保留在 metadata。存储的 train 不替代评估隔离。
7. selection 在 samples 的独立分支记录入选状态；稀疏重复关系和排除证据归该 selection。一对一 annotation 默认使用从相同 base 建立的平级分支，显式独立结果表按 05 发布，大特征独立存表。
8. 为 `sample_id` 建标量索引。不能把 Lance 内部行号或物理文件位置当永久 ID。
9. 已发布 manifest、main 与固定逻辑快照不可变；允许在 samples.lance 新增 annotation/selection 分支。引用必须固定 table/branch/version，不跟随 latest。
10. 发布前校验，失败不静默丢数据。原始全量转换、标注和特征任务都要明确处理与验证覆盖范围。
11. selection 明确音频区间决定；新来源须核实当前 bytes 与来源范围的关系。需要裁剪时由 selection 生成配套文本的实际片段样本，
    不覆盖 base、不重复应用上游已执行范围，也不由 codec 临时猜边界。规则与当前能力边界见
    [12 音频区间决策](specs/12-selections.md#音频区间决策与新数据集接入)。

## 4. 为什么用这种组织

Lance 分支继承基础数据文件，新增选择列独立写入；读取时按列投影，不需加载全部音频。
选择状态、重复裁决和特征计算分别版本化。codec、speaker、text 各自发布，按样本身份关联，
不通过物理行序推断对应关系。交付规则见 [12 Selection](specs/12-selections.md) 和 [13 Text](specs/13-text-features.md)。
训练端固定所消费的 manifest/table/branch/version；数据发布完成不等于训练读取已验收。

依据：[Lance branches](https://lance.org/guide/tags_and_branches/)、
[data evolution](https://lance.org/guide/data_evolution/)。具体版本与性能验证保存在 pipeline 仓库。

## 5. 长期不变量

- 调整 worker、批大小、输出分片、运行路径不会改变样本身份。
- 文本纠错不改变原音频；纯音频 codec 不因文本修订而失效。
- 质量 missing、failed、unsupported、skipped 与真实 0 分分别表达。
- speaker 的命名空间和可靠性明确；跨语言同名角色不能直接作为同音色克隆配对。
- tokenizer 权重、预处理、码本结构不同的 codes 不混作同一 token 空间。
- 每份交付可追溯到准确的音频样本、文本、标注和特征版本；被下游引用的快照及数据文件须保留。
- 规范中的将来能力与实际通过验收的能力分开记录；不因目录已创建就标为完成。
