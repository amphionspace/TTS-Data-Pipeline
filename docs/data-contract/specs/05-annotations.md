# 05 · 标注、质量分数与关联

## 默认：一对一结果追加版本化列

音质、语言判断、单份 ASR/文本修订等每个 sample 最多一个结果的任务，发布到 samples.lance 的新 struct 列。
列名 `ann__<task>__<run_id>`，task/run_id 限制为 `[a-z][a-z0-9_]{0,63}`，且不得包含双下划线。
列名显式带 run，禁止反复覆盖 quality/latest 等无版本字段。每个 run 的 manifest 指出 table、snapshot、column。

```text
samples.lance
sample_id | audio | text | ann__audio_quality__run_001
A         | ...   | ...  | {status:ok, input_fingerprint:..., result:{score:4.1}}
B         | ...   | ...  | null
C         | ...   | ...  | {status:failed, input_fingerprint:..., result:null}
D         | ...   | ...  | {status:ok, input_fingerprint:..., result:{score:0.0}}
```

外层 struct 为 null 表示该 run 未运行此样本。已运行结果包含：
`status`、`input_fingerprint`、`error_code`、`result`。result 使用任务专用固定 Arrow struct，
不把所有分数塞到 JSON；每个指标可独立有 status，需要时拆成不同任务。
ok 要求 result 非空、error_code 为空；failed/unsupported/skipped 要求 result 为空，失败须有 error_code。
不能用 NaN、空串、-1 或 0 编码未运行/失败；真实 0 分只能由成功结果表达。

## 关联如何完成

1. 任务读固定输入 snapshot，按批次取得 sample_id 和所需输入列；依赖修订文本等旧标注时，
   同时固定相应 run 与包含它的 snapshot，不能仅依赖原始 base snapshot。
2. worker 输出 sample_id + 输入指纹 + 结果，不假定返回顺序与基础顺序一致。
3. 发布前校验 sample_id 唯一、目标存在、指纹匹配、覆盖计数和结果值域。
4. 协调者用 Lance merge/add-columns 将新列关联到主表；worker 不各自并发修改主表 schema。
5. 校验新 snapshot 的行数不变、键不变、未覆盖行为空、原音频文件未因加列被复制。
6. 写不可变 run manifest 固定提交 snapshot；消费者必须从 manifest 读取该版本。

已有 `sample_id` BTREE 索引帮助选择性查询；批量 merge 是批处理，可能扫描键或持有中间状态。
不要求把音频全载入内存；读质量只投影 quality、language、speaker、duration 等小列。
计算任务在外部检查点中保存进度；完成后一次发布。后续补算未覆盖样本发布新 run，可声明继承哪个旧 run，
最终列包含新 run 的完整选定结果，避免训练临时拼多个半成品 run。

## 一对多 / view 结果

词时间戳、对齐事件、多个候选、关系边等放 `annotations/<task>/<run_id>/results.lance`。
共同字段：target_kind、target_id、item_id、input_fingerprint、status、error_code，加任务专用结果列。
(target_kind,target_id,item_id) 唯一，target_kind 固定为 sample 或 view；manifest 固定引用表与 snapshot。
view 的一对一任务也先用独立结果表；不得将多个 view 结果硬塞到父 sample 的单值列。
为 target_id 建标量索引；一对多结果只在训练构建或检查阶段关联，训练热路径使用构建好的记录。
一对多任务同时发布 targets.lance：每个已运行 target 一行，列为 target_kind、target_id、
input_fingerprint、status、error_code、item_count。ok 且 item_count=0 表示成功但无事件；
未运行 target 没有状态行，failed/unsupported/skipped 的 item_count 为 null 且无事件行。
results.lance 只存成功事件，status=ok；其计数必须与 targets.lance 对账。
manifest 的 tables.targets 与 tables.results 分别固定 release 相对路径和 snapshot，不能只靠“没有事件行”判断失败或未运行。
view 的一对一结果表不需要 targets.lance，结果行自身即状态行。


## 指标与版本

metric profile 固定指标名、定义、方向、单位、范围、模型权重、实现、适用语言/域、输入依赖。
上游评分先保留原值；正式结构化导入注明 upstream 与 unknown_version（若确实未知），不能伪造模型版本。
音质依赖音频与解码/裁剪；音文一致性同时依赖选用文本；文本纠错后旧一致性分数不能直接复用。
MOS、预测 MOS、DNSMOS、SNR、ASR error rate 分开命名，不默认合成通用总质量。

训练配方逐任务固定 run，分别定义 missing/failed/unsupported/skipped 的 keep/exclude/fallback 策略。
先硬过滤，再计算抽样权重；记录各来源、语言、speaker 的保留率。综合质量是独立派生任务，
必须固定校准、公式、输入 run 和缺值策略。
