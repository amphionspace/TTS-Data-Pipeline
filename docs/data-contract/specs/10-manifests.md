# 10 · Manifest、类型与兼容性

## 通用字段

每个发布都有不可变 manifest.json；JSON 使用 UTF-8、禁止 NaN/Infinity，时间使用 UTC ISO8601。
manifest 文件哈希由引用者计算，不将自己的哈希写进自身导致递归。

| 字段 | 用途 |
| --- | --- |
| contract_version | v0.1 |
| artifact_kind | base / annotation / view / feature / training_build / asset |
| status | 公开发布只接受 complete |
| dataset_id / release_id | dataset 产物的归属；build 可跨数据集，列出 inputs |
| storage_format | lance |
| storage_version | 实际 Lance 文件格式版本；与 contract version 分开 |
| table_path / lance_version | 表路径与固定整数 snapshot；列内标注还需要 column |
| schema_version / schema_sha256 | 基础或任务 schema 的语义版本与类型描述摘要 |
| rows / validation | 行数、通过的检查、各检查覆盖数量和局限 |
| inputs | 确切来源文件集合，或输入发布 manifest 哈希与 table snapshot |
| dependencies / code_sha256 | 软件和实现版本；模型任务还需 profile |
| finished_at | 发布时间 |

base 的 table_path 相对 release 根目录，固定 samples.lance。其他 dataset 产物的 table_path 也相对该 release 根目录。
所有 inputs/recipe 中的外部引用 table_path 和 manifest_path 相对统一根目录，不依赖调用者当前工作目录。
引用保存 manifest_sha256；一份 manifest 引用多个内部 snapshot 时，还需指定对应表/列，不能只凭 manifest 哈希猜版本。
training_build 自身的 table_path 相对该 build 根目录，固定 records.lance。
一对多标注使用 tables.targets/tables.results 两组 table_path/lance_version，schema 描述覆盖两张表。
文件系统绝对根从部署配置映射，不写进内容身份。
表内标注 manifest 声明 storage_kind=sample_column，其他结果表为 storage_kind=result_table。
一个 run 如果没有实际结果表，不创建空 results.lance。

## 基础发布额外字段

identity_scheme、source_snapshot_scope、source_selection、inputs 中源相对路径/完整哈希/大小；
excluded_source_files（即使为空也写出）保存所有明确排除的路径、大小、哈希与原因；inputs 只列实际选入文件。
逐记录排除保存 excluded_source_records（定位、源/音频哈希、条件与原因）及 rejected_rows；
源文件仍在 inputs 中，inputs.rows 是原始 footer 行数，不改为过滤后的数量。
显式代码修复迁移时，code_sha256 指协调者执行版本；checkpoint_code_versions 保存实际批次代码映射，
batch_manifests 的 code_version 为对应代码映射的 canonical digest，code_migration 记录迁移依据。
统计 duration_seconds、audio_bytes、languages、sample_rates、source_splits、speaker 统计。
shards 列出基础 fragment 数据文件相对路径、行数、文件大小、SHA256；fragments 元数据属于内部检查点或可选诊断字段。
表的版本元数据/索引由 snapshot 引用；shards 不是用户手工拼表的读取入口。
output_bytes 在基础转换 manifest 中表示列出的基础数据文件总 bytes，不包含引擎索引、版本元数据和后续派生列。
batch_manifests 仅是执行审计摘要，不能成为读取发布的必要依赖。

### 基础发布的审计字段与历史兼容

新写出的基础 manifest（direct 与 bulk）均包含 rejected_rows、excluded_source_records、
checkpoint_code_versions、code_migration、finalization：

- rejected_rows 为此次转换明确拒收的记录数，零表示确认没有；与 excluded_source_records 长度一致。
  上游处理时已删除、未进入此次 inputs 的记录不计入此数。
- checkpoint_code_versions 为实际 checkpoint 代码映射；direct 无聚合 checkpoint 时为 {}。
  不能把协调者的 code_sha256 推测成所有历史批次的代码版本。
- code_migration 无迁移时为 null。
- finalization 为 bulk 收尾的实测统计；direct 不适用或历史未记录时为 null，不伪造零耗时。

早期 v0.1 已发布 manifest 可能缺少以上字段。发布文件保持不可变，不为补字段改变其 SHA256。
pipeline 的 read_base_manifest(release) 提供统一的内存读取视图：缺失字段返回 null；
若 excluded_source_records 已有明确列表而 rejected_rows 缺失，则从列表长度补出数量。
null 表示未知/未记录（finalization 也可能不适用），与 0、[]、{} 各自的已知含义不同。
直接解析历史 JSON 的外部消费者必须使用同样的缺省规则；引用哈希始终计算原文件 bytes，
不得对补齐后的内存对象计算哈希冒充原 manifest 哈希。兼容读取不会补造历史审计证据。

## Run 与 build 额外字段

run 固定 task、run_id、target_kind、profile_id/profile、输入依赖与 schema、覆盖数、各 status 数、未运行数。
仅对 storage_kind=sample_column：rows 表示非 null 结果数，table_rows 表示所引用 samples snapshot 的总行数。
这类 run 的 coverage.total_targets 表示声明的任务范围大小，必须 ≤ table_rows；范围外行和范围内未运行行均不写结果，
两者通过 manifest 固定的 selection 或选择表区分。coverage 的各状态加 missing 等于 total_targets。
独立一对一结果表 rows 是物理结果行数；一对多 rows 是事件数，并另记 target_rows 和目标状态统计。
每个任务定义 result 类型/指标范围。manifest 的 snapshot/column 是结果位置，run_id 不是查询 latest 的别名。
feature 固定 kind、完整 profile、target_kind、输出形状/dtype/轴/数组哈希序列化以及全部终态数量。
codec 的 codes 与 speaker 的 embedding 分别按 06/11 定义；profile hash 只覆盖完整 profile 对象。
feature 不继承 sample_column 的 table_rows 上限。输入别名和 selection 固定生成范围，具体字段见 06 第 8 节。
selection.available_target_rows 是目标 samples 或 views 快照的行数；可选 parent_sample_rows 仅为审计统计。
subset 的 targets.lance 由同一 run manifest 的 selection.table 固定路径、snapshot、schema 摘要和行数，随 run 发布并保留。
feature 发布必须 rows = selection.target_count = coverage.total_targets = ok + failed + unsupported + skipped，missing=0。
完整终态记账仍可包含失败，训练只用所需特征均为 ok 的记录。
execution 记录实际软硬件、CPU/GPU 并发和 batch 参数，validation 记录逐项覆盖数与边界；
示例见 [feature manifest](../examples/feature-manifest.example.json)。
build 固定 recipe 哈希、全部 inputs、storage_layout、模型输入协议、候选/排除/配对计数、时长/token 统计。

## Schema 演进

schemas/arrow-schemas.json 是结构化 Arrow 类型描述，不是 HF Features，也不是通用 JSON Schema 校验器。
它由 pipeline 的 schema 函数生成。业务必填、唯一性、引用、状态与数值范围由验证器实施；Arrow nullable 不等于业务允许 null。
基础列不可改名或改义；新的任务使用新列/新表及独立 schema。添加派生列不改变基础 record_revision。
改采样时间单位、身份算法、字段含义或 codec token 空间必须显式新 profile/schema/release，不在旧版本下静默替换。

旧 HF Parquet 输出约定停止作为发布规范；已有原始 HF 文件仍是合法输入。
可保留 HF 导出作为单独工具，但不能把 Lance 存储伪称可以直接 load_dataset("parquet")。
所有示例中的占位 ID/profile/版本必须替换并通过验收后才能用作生产 recipe。

自包含来源使用 source-file-v1；外部语义依赖使用 source-unit-v1，算法见 03。
每个 inputs 项的 semantic_dependencies 固定依赖文件的相对路径、大小和完整 SHA256；
它们不计入待转换音频文件数，但必须参与身份、输入清单摘要及恢复校验。
