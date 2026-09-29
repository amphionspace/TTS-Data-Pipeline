# 统一数据契约 v0.1

## 1. 目标和处理顺序

```text
原始数据（只读）
    ↓ 每个来源一个 adapter
samples.lance：原音频 + 基础文字/语言/speaker/来源
    ├─ selection 分支 → 原行 + 入选主原因/多重原因，不复制音频
    ├─ annotation 平级分支 → 固定任务结果；一对多结果另存表
    ├─ views → 明确音频区间
    └─ 多 codec / embedding profile → 独立 Lance 特征表
              ↓ 固定 selection、特征快照与定位绑定
builds/<build_id>：可复用训练输入绑定
              ↓ training_plans：独立采样配方，不因改权重复制特征
              ↓ 按 token 长度组 batch
Qwen 风格 TTS 训练（模型从头初始化）
```

原始数据、统一数据、派生结果和训练 build 各有职责。原始来源不被修改；统一音频不因训练实验重复解码存储；
训练读取阶段不计算质量分数、不运行 codec 推理、不做全库 join。
冻结 speaker encoder 可直接读取缓存 embedding；未来解冻则读取原音频/view，按当前 frontend 在线提取，
不把某一种 mel 规定为统一层必备产物。参见 [Speaker 两条路径](specs/11-speaker-embeddings.md)。

## 2. 固定决定

1. 统一存储使用本地 Lance 表，不要求启动数据库服务。HF Parquet 可以作为输入或将来的交换导出格式。
2. 根目录为 `/workspace/data/DATA-TTS-UNIFIED`。按 dataset 管理发布，每个 release 的主表是 `samples.lance`。
3. 首次发布名为 `v0.1`。主表在物理上由 Lance 管理多个数据文件；运行 batch 不是数据目录或训练读取单元。
4. 音频保留原始编码 bytes、采样率和声道；不在基础接入时重采样、归一化、重编码。
5. 基础列按 27 列 schema 写入；缺失文本、语言、speaker 使用 null。后续标注不覆盖基础列。
6. 全部基础记录 `source_split=train`；上游 split/config/tier 保留在 metadata。存储的 train 不替代评估隔离。
7. selection 在 samples 的独立分支记录入选状态；稀疏重复关系和排除证据归该 selection。一对一 annotation 使用从相同 base 建立的平级分支，大特征独立存表。
8. 为 `sample_id` 建标量索引。不能把 Lance 内部行号或物理文件位置当永久 ID。
9. 已发布 manifest、main 与固定逻辑快照不可变；允许在同一 samples.lance 内新增分支文件。引用必须固定 table/branch/version，不跟随 latest。
10. 发布前校验，失败不静默丢数据。原始全量转换、标注任务和训练构建都要明确处理与验证覆盖范围。

## 3. 为什么用这种组织

Lance 分支继承基础数据文件，新增选择列独立写入；读取时按列投影，不需加载全部音频。
选择状态、重复裁决、特征计算和训练采样分别版本化；不引入第二份带完整文本/音频的目录。
首版采用 self speaker 参考。codec 与 speaker 各自发布，训练优先验证按索引引用的布局；
未经端到端吞吐验收不宣称训练读取就绪。具体见 [12 Selection](specs/12-selections.md) 和 [07 训练](specs/07-training-builds.md)。

依据：[Lance branches](https://lance.org/guide/tags_and_branches/)、
[data evolution](https://lance.org/guide/data_evolution/)。具体版本与性能验证保存在 pipeline 仓库。

## 4. 长期不变量

- 调整 worker、批大小、输出分片、运行路径不会改变样本身份。
- 文本纠错不改变原音频；纯音频 codec 不因文本修订而失效。
- 质量 missing、failed、unsupported、skipped 与真实 0 分分别表达。
- speaker 的命名空间和可靠性明确；跨语言同名角色不能直接作为同音色克隆配对。
- tokenizer 权重、预处理、码本结构不同的 codes 不混作同一 token 空间。
- 每次训练能指出准确的音频/视图、文本、标注、特征版本与抽样配方。
- 规范中的将来能力与实际通过验收的能力分开记录；不因目录已创建就标为完成。
