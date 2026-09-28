# 统一数据契约 v0.1

## 1. 目标和处理顺序

```text
原始数据（只读）
    ↓ 每个来源一个 adapter
samples.lance：原音频 + 基础文字/语言/speaker/来源
    ├─ 一条 sample 对一个结果 → 追加版本化标注列
    ├─ 切片/对齐/关系等一对多结果 → 独立 Lance 表
    └─ 多 codec / embedding profile → 独立 Lance 特征表
              ↓ 固定快照、检查指纹、质量筛选、参考配对
builds/<build_id>：训练记录 + 固定配方 + 采样信息
              ↓ 按 token 长度组 batch
Qwen 风格 TTS 训练（模型从头初始化）
```

原始数据、统一数据、派生结果和训练 build 各有职责。原始来源不被修改；统一音频不因训练实验重复解码存储；
训练读取阶段不计算质量分数、不运行 codec 推理、不做全库 join。

## 2. 固定决定

1. 统一存储使用本地 Lance 表，不要求启动数据库服务。HF Parquet 可以作为输入或将来的交换导出格式。
2. 根目录为 `/workspace/data/DATA-TTS-UNIFIED`。按 dataset 管理发布，每个 release 的主表是 `samples.lance`。
3. 首次发布名为 `v0.1`。主表在物理上由 Lance 管理多个数据文件；运行 batch 不是数据目录或训练读取单元。
4. 音频保留原始编码 bytes、采样率和声道；不在基础接入时重采样、归一化、重编码。
5. 基础列按 27 列 schema 写入；缺失文本、语言、speaker 使用 null。后续标注不覆盖基础列。
6. 全部基础记录 `source_split=train`；上游 split/config/tier 保留在 metadata。存储的 train 不替代评估隔离。
7. 同一 sample 的质量结果用版本化 struct 列关联；一对多结果与大特征各建表。所有关联使用业务 ID 和输入指纹。
8. 为 `sample_id` 建标量索引。不能把 Lance 内部行号或物理文件位置当永久 ID。
9. 已发布的逻辑快照不可变。追加列产生新 Lance snapshot；旧发布 manifest 固定旧 snapshot，不自动跟随 latest。
10. 发布前校验，失败不静默丢数据。原始全量转换、标注任务和训练构建都要明确处理与验证覆盖范围。

## 3. 为什么用这种组织

Lance 提供列选择、索引查询、快照以及补充列的存储机制，适合长期补充 quality/ASR 等结果。
将分数写成同一表的新列后，读取 sample 与分数不再需要运行时查找多个手工 sidecar。
标注计算仍需读输入，合并也需要键匹配；这些操作不承诺零扫描或零内存。
大型 codec 单独存表，允许独立生成、版本选择和回收；训练 build 提前选择结果并准备读取布局。

依据：[Lance data evolution](https://lance.org/guide/data_evolution/)、
[distributed write](https://lance.org/guide/distributed_write/)、
[read/write](https://lance.org/guide/read_and_write/)。具体性能以本机真实数据验证为准。

## 4. 长期不变量

- 调整 worker、批大小、输出分片、运行路径不会改变样本身份。
- 文本纠错不改变原音频；纯音频 codec 不因文本修订而失效。
- 质量 missing、failed、unsupported、skipped 与真实 0 分分别表达。
- speaker 的命名空间和可靠性明确；跨语言同名角色不能直接作为同音色克隆配对。
- tokenizer 权重、预处理、码本结构不同的 codes 不混作同一 token 空间。
- 每次训练能指出准确的音频/视图、文本、标注、特征版本与抽样配方。
- 规范中的将来能力与实际通过验收的能力分开记录；不因目录已创建就标为完成。
