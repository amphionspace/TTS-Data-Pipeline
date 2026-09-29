# DATA-TTS-UNIFIED · v0.1

这是多语言、音色克隆 TTS 的统一数据契约。**统一存储使用 Lance，发布版本使用 v0.1。**
基础音频与模型无关；训练采用 Qwen3-TTS 结构和 tokenizer，基础模型重新初始化。

本目录保存规范、机器可读类型描述和示例。数据放在同一根目录的 `datasets/`、`selections/`、`builds/`、`training_plans/`、`assets/`；
只在产生实际数据时创建这些目录。运行日志、核查报告、实现差距留在 tts-data-pipeline 仓库。
本契约替代此前 HF Parquet 输出、手工对齐 sidecar、按执行 batch 组织数据的草案。

## 从哪里开始

先读 [总约定](CONTRACT.md)，再看 [完整目录](specs/01-layout.md) 和 [选择分支](specs/12-selections.md)。

| 文件 | 规定什么 |
| --- | --- |
| [01 目录与生命周期](specs/01-layout.md) | dataset、release、run、profile、build 的位置与发布边界 |
| [02 基础字段](specs/02-base-schema.md) | 27 个基础字段、音频、文本、语言、speaker 与未知值 |
| [03 身份与版本](specs/03-identity.md) | 来源身份、内容指纹、Lance snapshot、失效与复现 |
| [04 视图与时间轴](specs/04-views-timelines.md) | 长录音、片段、对话、说话人范围、坐标 |
| [05 标注与质量](specs/05-annotations.md) | 平级任务分支、结果状态、位置对齐、文本修订和依赖保护 |
| [06 Codec 与特征](specs/06-codecs.md) | 公共特征身份、codec 数组、多 profile、生成/恢复/索引与覆盖验收 |
| [07 训练构建](specs/07-training-builds.md) | 固定输入、参考配对、筛选、采样、分布式读取 |
| [08 接入与验收](specs/08-ingestion-validation.md) | adapter、完整性、全量回读、失败与断点恢复 |
| [09 Lance 操作](specs/09-lance-operations.md) | 查询、索引、并发写、快照保留与清理 |
| [10 Manifest 与兼容性](specs/10-manifests.md) | 发布清单、版本固定、schema 与 profile 演进 |
| [11 Speaker embedding](specs/11-speaker-embeddings.md) | 逐片段 embedding、冻结/在线 encoder、参考配对；不强制保存 mel |
| [12 Selection](specs/12-selections.md) | 分支原因列、完整排除继承、全局去重、原子发布和特征关联 |
| [类型描述](schemas/arrow-schemas.json) | 与 Python schema 一起生成的 Arrow 类型树 |
| [示例](examples/README.md) | 仅用于理解约定，示例分数与 codes 不是模型结果 |

## 版本边界

- `contract_version`、基础 `schema_version` 和首次 `release_id` 均为 `v0.1`。
- `identity_scheme` 独立于发布版本：自包含文件用 source-file-v1，外部语义依赖用 source-unit-v1。
- Lance 的整数 snapshot version、Lance 文件格式版本和 pylance 软件版本是存储实现信息，不能充当发布版本。
- 一份规范写明某项能力，不代表其执行器已实现。验收状态只看 pipeline 的测试与实现记录。

文档源稿：`tts-data-pipeline/docs/data-contract/`；部署副本：`/workspace/data/DATA-TTS-UNIFIED/`。
变更先修改源稿、检查代码和例子，再同步部署副本。
