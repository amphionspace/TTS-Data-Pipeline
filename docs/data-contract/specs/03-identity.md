# 03 · 身份、指纹与快照

## 四个版本维度

| 名称 | 负责什么 | 例子 |
| --- | --- | --- |
| contract/schema version | 字段语义和约束 | v0.1 |
| dataset release | 来源范围和基础映射 | libriheavy/v0.1 |
| run_id / profile_id | 一次派生发布 / 完整计算配置 | 质量任务 run、codec profile 摘要 |
| Lance snapshot version | 一张物理表的提交版本 | 整数 2、3；独立于 release_id |

外部输入引用（inputs）必须包含相对于统一根目录的路径、显式 branch（main 用 null）、整数 snapshot version 和输入 manifest 的哈希。
产物 manifest 自身的 table_path 则相对于所属 release 根目录，详见 10。
同一个 v0.1 可以有多个派生 snapshot；训练固定具体版本，不依赖 latest。

## 来源身份算法

canonical_json：UTF-8，ensure_ascii=False，sort_keys=True，separators=(",", ":")，禁止 NaN/Infinity。
摘要均为小写 SHA256 十六进制；哈希文件时覆盖完整 bytes。

```text
identity_scheme = "source-file-v1"
source_snapshot = "source-file-v1:sha256:" + SHA256(canonical_json({
  "path": source_file_relative_posix_path,
  "sha256": source_file_sha256
}))
sample_id = SHA256(canonical_json([dataset_id, source_snapshot, source_key]))
audio_sha256 = SHA256(audio.bytes)
record_revision = SHA256(canonical_json(27 个基础字段中除 audio、record_revision 的字段))
```

source-file-v1 只适用于自包含来源文件，包括内嵌音频的 Parquet 和配对完整的 tar。
依赖单独文本/评分文件的来源使用 source-unit-v1：

```text
source_snapshot = "source-unit-v1:sha256:" + SHA256(canonical_json({
  "source": {"path": source_relative_path, "sha256": source_sha256},
  "dependencies": [{"path": dependency_relative_path, "sha256": dependency_sha256}, ...]
}))
```

依赖列表按相对 POSIX path 排序去重，覆盖所有影响基础字段的外部文件。
inputs 每项记录 semantic_dependencies（path、bytes、sha256），发布前检查是否变化；
恢复任务还校验计划固定的依赖内容哈希。两个 scheme 不改变 sample_id 的组合公式。
共享文件变化会改变依赖它的所有来源单元身份；这是保守失效，不按变动行猜测继承。
此方案用于原始来源的语义依赖；后加标注遵循独立 run/revision，不修改 base 身份。
不能仅哈希音频冒充完整快照。
来源文件内容或相对名变化会改变其中的 sample_id；移动根目录、改变 worker/批大小/输出文件不会改变它。
单次运行 inputs 清单的摘要不是行级 source_snapshot。上游 ID 的唯一性范围需逐 adapter 说明。

source_locator_json、metadata_json 是字符串，序列化变化可能影响 record_revision；写出时固定序列化。
audio.path 仅显示用途，不参与当前 revision。后加的标注列不参与基础 revision。
相同 audio_sha256 不要求合并 sample；来源身份与字节重复关系分别保存。

## 派生结果身份

profile_id = SHA256(canonical_json(完整 profile))；profile 不含自身 ID、机器路径、GPU 编号或时间戳。
run_id 是一个 release/task 下唯一的不可复用名称；必须记录 profile、输入 snapshot、选择范围、代码版本。
同一 run 重试是完成同一次发布；发布后改变结果必须新建 run。

input_fingerprint = SHA256(canonical_json(任务声明的输入对象))，该对象至少明确：
音频哈希、样本身份、实际输入区间与 timeline profile、所用文本/说话人修订（若依赖）、处理 profile。
音文一致性依赖文本，纯音质/codec 不应加入无关文本以造成无意义失效。

selection 裁剪后须生成新的样本身份，绑定父音频、实际区间、处理方法和产物摘要。
具体产物 schema 与身份算法须随裁剪执行器实现并验收；当前不支持将父样本 ID 直接用于不同片段。

feature_key = SHA256(canonical_json(["feature-v1", audio_sha256, timeline_profile_id,
  start_frame, end_frame, profile_id]))；整条音频也填写明确区间。
多次执行相同输入/profile 可以共享 feature_key；实际输出还用 codes_sha256 或 embedding_sha256 验证，不能仅凭 key 宣称确定性。

## 关联约束

base：sample_id 唯一。selection 裁剪产物还须核验父样本、父音频摘要与原始区间。
一对一标注：run 内每个 target 最多一个最终结果。一对多标注：每个 target 可有多行，
run 内 (target_kind,target_id,item_id) 唯一；同一 target 的任务完成状态还需单独表达，见 05。
特征：run 内 (target_kind,target_id) 唯一；feature_key 可重复以表达多个来源指向相同内容。
所有新表在发布时检查重复键、孤立引用和输入指纹。Lance 标量索引不提供外键或唯一性约束。

Lance `_rowid`、row index、fragment ID 只在指定 snapshot 下作为执行定位使用，不能替代上述业务键。
新来源或映射迁移如需沿用旧标注，必须验证身份映射和输入指纹，禁止按文件名自动继承。

codec/speaker 特征的 input_fingerprint 对象、encoder_input_sha256 的波形定义和数组摘要格式
固定在 [06](06-codecs.md)。这两类纯音频任务不依赖文本和 speaker 标签修订。

## 上游 short 映射的接入身份

Emilia2 使用 `identity_scheme=source-transform-v1`，其 source_snapshot 为：

```text
"source-transform-v1:sha256:" + SHA256(canonical_json({
  "path": source_tar_relative_path,
  "sha256": source_tar_sha256,
  "ingest_profile_id": SHA256(canonical_json(ingest_profile))
}))
```

sample_id 仍按 `[dataset_id, source_snapshot, source_key]` 计算，source_key 为上游 short ID。
源 tar 同时包含原始音频和带整数区间的 JSON。profile 固定解码器、区间规则、输出编码、
候选优先级、实现摘要和依赖版本；原始 short 也使用同一明确的接入身份方案。
因此改变裁剪、编码或来源会改变身份，改变 worker 数或分片不会改变身份。
此规则只描述上游对象到基础 short 的映射；不复用为 selection 派生身份协议。
