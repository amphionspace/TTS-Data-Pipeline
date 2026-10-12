# Emilia upstream transcription import — 2026-10-10

状态：真实样本测试通过，已启动全量导入。正式是否可用以全局 `status=complete` manifest 为准。
本报告验证格式、身份关联及导入保真，不评估上游 ASR 的识别准确率。

## 输入与范围

- 基础输入：`datasets/emilia/v0.1/samples.lance` version 2，共 40,264,231 条。
- 上游：`DATA-TTS/Emilia-Refined-Metadata/emilia_1.0/`，2,360 个 JSON，120,605,700,870 bytes。
- 基础 manifest SHA256：`0d973d59af4cede097b84a1d13c917033ded879124ac1b05eaa23d58672ed1ba`。
- 全量 metadata 扫描的各语言条数与 base 一致；目录、JSON 语言及 ID/wav 语言前缀冲突均为零。
- 发现 31 条纯标点文本，保留转写，交由 selection 决定资格；本次不进行文本清洗。
- 文件内重复 ID、缺失/非法文本类型、空文本及已检查的模型协议标记均未发现。
  全量导入仍逐条关联基础身份，并核对每个文件实际匹配数，预检计数不替代身份验证。

| language | rows |
| --- | ---: |
| de | 653,109 |
| en | 18,136,271 |
| fr | 543,710 |
| ja | 869,662 |
| ko | 92,182 |
| zh | 19,969,297 |

## 格式与语义

使用与四游戏已发布结果相同的 `qwen-asr-transcription-v2` Arrow schema 和 `sample_table` 布局。
schema SHA256 为 `d79310dcd791c3c4b8ebf34fb2c7715d2972370966aff9f9626daf63d92f38c1`。
新增 `qwen3-asr-1.7b-emilia-metadata-v1` profile，`execution_kind=upstream_metadata_import`。
`text` 原样保存，JSON `language` 规范成小写语言代码，作为 `result.language`；来源语言保留。
未提供的请求证据、实际推理范围和指定语言候选字段为空；不导入 timestamps。
详细映射和指纹定义见 [05 · 上游 transcription 导入](../data-contract/specs/05-annotations.md#上游-transcription-导入)。

metadata 文件名对应原始 tar，结合该 tar 的 source_snapshot 与记录 id 匹配 sample_id。
记录 ID 中的 `Bxxxxx` 不一定与 tar 编号相同，不能据此猜测来源文件。
不调用模型、不读取音频列，不改 samples、旧 annotation、selection 或 merged。

## 一万条真实样本测试

从每种语言首、中、尾 metadata 文件及一份包含纯标点记录的文件抽样，共 19 个文件、10,000 条。
包含短/长文本和纯标点。语言分布为 de 1,521、en 2,141、fr 1,491、ja 1,731、ko 1,385、zh 1,731。
抽样基于真实 base 的 ID 索引，建立 artifacts 内的明确抽样 fixture，不发布到正式 unified。

验证全部通过：

- 10,000 条均为 ok；text 和 language 与上游逐条相等，空白及标点未被修改。
- sample_id 顺序及音频摘要与固定 fixture base 一致。
- Arrow schema 与四游戏已发布 Lance 表完全相等，不只比较字段名。
- 未知请求证据和范围字段均为 null。
- 4 worker 转换后模拟一个未提交检查点，以 2 worker 恢复：只重算该片，全部 Parquet 内容摘要不变。
- 再以 1 worker 独立转换和发布，两套结果全字段相等。
- staged Lance 全字段回读、ID 索引和 complete manifest 发布均通过。

另有 52 项相关测试通过：metadata import 8 项、已有 transcription 21 项、contract/feature contract 23 项。
覆盖缺失身份、错误 wav、重复 metadata ID、输入变化、损坏 checkpoint、非法语言/文本、空转写及发布恢复。

测试发现 Lance 12 在 CPU/IO 线程池均设为 1 时建立 BTree 索引停滞。
相同 10,000 行复现，在 CPU=2、IO=4 下索引与 tag 创建约 0.6 秒完成；入口默认改为该配置。
修正后真实样本发布和独立单 worker 对照通过。抽样的 JSON 解析开销与全量不同，不用抽样吞吐直接推算全量时间。

## 全量执行与读取入口

首次全量 run `tts-ann-transcription-20261010T171514bjt-import`（8 个 CPU worker，2,603 个基础分片）完成后，
发现 run_id 后缀不符合 05 的 `bjt-NN` 规则（导入器硬编码 `-import`）。修正导入器后于 2026-10-12 以相同输入重跑，
32 个 worker，导入约 4 分钟、发布约 6 分钟；新旧两表 40,264,231 行的 sample_id、输入指纹、status、error_code、result
逐行完全一致，profile_id 相同。旧 run 已删除，工作目录为 `artifacts/emilia-transcription-import-20261012/`。

发布后的全局入口：

```text
annotations/transcription/tts-ann-transcription-20261012T092945bjt-01/manifest.json
datasets/emilia/v0.1/annotations/transcription/tts-ann-transcription-20261012T092945bjt-01/results.lance
```

读取时固定 manifest 中的 table version，按 sample_id 关联。是否采用文本由新 selection 显式决定。
恢复命令见 [ASR 标注执行说明](../annotation.md#emilia-上游转写导入)。

原始验证证据：

- `artifacts/emilia-refined-precheck-20261010/language-report.json`：全量 metadata 检查及文件 SHA256。
- 同目录 `input-stability-check.json`：检查结束时源文件清单、大小/mtime 及 base manifest 未变化。
- `artifacts/emilia-transcription-import-test-20261010/report.json`：真实样本格式、内容及恢复验证。
- 全量 work 的 `plan.json` 固定源文件、base 快照、实现和 profile；完成后 `validation.json` 保存分语言/文件覆盖。
