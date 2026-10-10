# ASR 标注

本轮生成独立 transcription annotation，不修改 samples 原字幕、selection 或现有 features。
首轮全量范围为原神、星铁、鸣潮及已完成 samples 发布的 Zenless；Galgame 不在该轮内。
Galgame 曾单独做一万条抽样转写与原文的 WER/CER 对照；随后按用户扩大范围的指令纳入全量。
已完成结果见 [Galgame 一万条对比报告](design-review/galgame-transcription-comparison-2026-10-10.md)。
整体质量模块规划仍见 [实验 contract](data-contract/experiments/annotation-v1.md)。
四个游戏的独立音频语言检测另见 [FireRedLID 执行说明](spoken-language.md)，不覆盖 ASR 语言。

## 2026-10-10 扩展范围

新增任务覆盖全部已发布且未完成 transcription 的 13 个数据集，共 133,696,192 条；
明确排除 Emilia2，四个已完成游戏不重复执行。Galgame 包含在本次范围内。
具体输入见 `artifacts/asr-unannotated-20261010/scope.json`，固定版本见该目录的 `work/plan.json`。
FireRedLID 正式任务继续暂停。新任务的自动恢复和每小时监督见
[监督说明](asr-unannotated-supervision.md)。

使用 `qwen3-asr-1.7b-base-v2` profile，固定 Qwen 支持的来源语言路由。
继承 samples 发布时的 ID 唯一性验证，不对全集重新去重；prepare 流式计算有序 ID 摘要及分批任务，
发布按行数、ID 顺序及全字段回读验证。数据结果仍为每个数据集、每个 run 一张单表，保留失败行。

## 固定输入与执行

| dataset | 基础行数 |
| --- | ---: |
| genshin_voice | 654,252 |
| starrail_voice | 403,437 |
| wutheringwaves | 78,777 |
| zenless_voice | 406,720 |
| 合计 | 1,543,186 |

读取各 v0.1 base manifest 固定的 main 快照，不以旧 selection 或 merged 作为全集。
入口为 `scripts/annotate_transcription.py`，代码位于 `src/tts_data_pipeline/annotations/qwen_asr/`。
prepare 固定输入 manifest/快照、目标顺序、权重/配置/词表摘要、客户端代码和依赖；run 使用同一计划恢复。

```bash
python scripts/annotate_transcription.py prepare \
  --root /workspace/data/DATA-TTS-UNIFIED \
  --model-root /workspace/model/Qwen3-ASR-1.7B \
  --work /path/to/new-work --batch-size 512
python scripts/annotate_transcription.py run --work /path/to/new-work --workers 128
```

客户端完成 CPU 读取、解码和重采样，GPU 推理由用户已有的
`http://127.0.0.1:18101/v1` 服务负责。默认 128 并发、600 秒网络 timeout。
进度见 work/status.json，发布完成后读取全局 annotation manifest；命令示例不代表任务完成。

## 推理与接口语义

使用 vLLM 0.18.0 的 /audio/transcriptions，一次完整发送一条音频。
完整解码后 FP32 声道均值、scipy resample_poly 至 16 kHz、FLOAT WAV 传输；客户端不裁剪或补齐。
服务自动处理长音频，已用 37.6、82.6、146.1 秒真实样本验证 30 秒之后的内容保留。
服务内部块没有可靠时间戳，不推测每块或每个词的时间坐标，只记录完整原生输入范围。
流式输出保留原始响应，移除各段协议标记后用换行连接正文，检查结束状态与连接完整性。

主转写使用自动语言识别，不传原字幕或专名词表。自动语言与已知配音语言冲突时，
额外请求指定来源语言的候选，单独保存，不覆盖主转写；来源语言未知时不猜测。
原字幕仍从固定基础快照读取，结果保存原字幕的 canonical JSON 摘要。

当前版本接收但忽略 language；实际追加 Qwen 语言前缀的是 to_language。
这是当前服务适配行为，不是所有 ASR API 的通用规则。
实测把韩语错指定为英语会得到拉丁字母转写，把英语错指定为韩语可能产生译文。
指定语言输出的标签不能作为独立语言检测证据。
依据：[vLLM 0.18.0 Qwen3-ASR](https://github.com/vllm-project/vllm/blob/v0.18.0/vllm/model_executor/models/qwen3_asr.py)。

ASR 仍可能把 Shenhe 写成 Shanha 等专名错误；本轮按用户要求暂不处理专名提示。
小测证明工程链路可用，不是总体准确率验收，不把 ASR 当作绝对真值。

## 存储与发布

```text
annotations/transcription/<run_id>/manifest.json
datasets/<dataset_id>/v0.1/annotations/transcription/<run_id>/
    results.lance
```

遵循正式 05 的 sample_table，schema_version=qwen-asr-transcription-v2。
每个样本一行，顶层为 sample_id、input_fingerprint、status、error_code、result。
result 为可空 struct，保存音频身份、完整输入范围、ASR 文本/语言、
来源语言及原文摘要、请求波形帧数/摘要、语言冲突和候选、原始响应。
失败也保留一行，result=null；成功但 text="" 是另一种状态，不把空文本当失败或训练合格。
语言候选与主转写在同一个 result 中，主转写不会被覆盖；候选失败信息保留在候选响应内。
manifest 每个 output 使用单个 table 引用，不再使用 tables.targets/tables.results。

旧的 qwen-asr-transcription-v1 双表和私有检查点格式保留。已发布版本转成单表：

```bash
python scripts/annotate_transcription.py migrate-tables \
  --root /workspace/data/DATA-TTS-UNIFIED \
  --source-manifest /path/to/v1/manifest.json \
  --work /path/to/migration-work
```

迁移只读取固定的原 annotation 快照，不解码音频、不调用模型。
使用新的 run_id，保留旧版；逐条验证 ID/指纹/状态，所有字段完整回读，建立 sample_id 索引后再发布。
layout_migration 记录原 manifest 路径和摘要；profile/profile_id/input_fingerprint 与原 ASR execution 保持一致，
publication_code_sha256 单独记录发布实现。后续新 prepare/run 默认发布 v2 单表。

模型未提供可靠置信分数，不制造 score。空转写可以是成功执行，不代表训练合格，不回退原字幕。

解码失败和输出截断明确记账；临时连接故障、408/429/500/502/503/504 自动重试，
模型变化、身份冲突、未知请求拒绝停止执行，不伪装成大量坏样本。
已验证批次可从固定计划和冻结代码恢复。
Parquet 仅作为私有检查点；全部完成后逐表回读、建立 ID 索引、固定快照及保护 tag，
最后原子写入全局 complete manifest。未发布结果不能供训练消费。

采用新文本仍需新的 selection，以及 text/merge 的元数据绑定实现，见
[contract 复核](design-review/contract-review-2026-10-09.md)。

## 验证证据

`artifacts/game-asr-pilot/` 保存测试输入清单和结果（可从固定 base 重建的导出 WAV 已清理）：

- 初始 32 条涵盖中英日韩、普通字幕、疑似错配/模板及缺字幕，均返回结果。
- 加入三条长音频的 35 条端到端验证完成独立 Lance 发布；注入中断后恢复，检查点未改变。
- Zenless 256 条、128 并发均成功落盘，另存 12 条语言冲突证据。
- wrong-language.json 是未生效 language 参数对照；to-language-comparison.json 是实际语言前缀对照。
- 存储测试覆盖空结果表、损坏检查点拒收、源音频身份冲突和发布中断恢复。

正式运行记录保存在对应 work；不要把 pilot 的 complete 误读为正式全量完成。

原始 v1 正式 run：`tts-ann-transcription-20261009T191644bjt-01`。
冻结代码与日志：`artifacts/game-asr-20261009/`；计划和进度：该目录下 `work/plan.json`、`work/status.json`。
源代码后续变化不影响已启动进程；恢复应使用同目录冻结代码。原始 v1 已于 2026-10-10 10:51（北京时间）完成发布，共 1,541,304 成功、1,882 失败。

CPU 波形统计独立执行，见 [audio_stats 运行说明](audio-stats.md)。


## 自动记录与恢复

新全量客户端通过 `run --auto-resume --workers 128` 启用自动恢复（历史游戏 run 使用 64）：

- 每次请求超时仍为 600 秒。连接/读取中断、未完整结束的响应流及临时 HTTP 状态最多尝试 3 次，间隔 2、5 秒。
- 重试用尽后，任务保存 `recovering` 状态及 `recovery-events.jsonl`，60 秒后自动校验并恢复同一计划。
- `supervisor.lock` 覆盖处理及恢复等待，避免两个恢复进程重复执行同一任务。
- 每次单条失败即时追加并刷盘到 `work/failures.jsonl`，包含时间、ID、输入指纹、原因、阶段和可得的原始错误/截断输出。
  原来的已落盘失败会在恢复时自动汇入日志并标记 origin=checkpoint，不依赖人工检查。
- 失败日志是执行事件，重试同一条可以有多条事件；正式目标状态以验证后的 checkpoint/targets 为准。
  主转写失败、语言候选失败和基础设施异常用 stage 区分，不能把日志行数当作失败样本数。
- 已复现的极短全零音频（请求少于 160 个 16kHz 采样点）遇到 Qwen3ASRProcessor 的 HTTP 400 拒绝时，
  记为 `failed/asr_audio_preprocessing_rejected` 并继续。仍发送真实音频，不补零、不生成假转写。
  其他未知 400、模型配置和来源冲突保持显式停止，避免系统性故障变成大量坏样本。
- `asr_output_truncated` 是未完整结束的模型输出，不作成功转写，不作为连接故障无限重试。

2026-10-09 恢复版本位于 `artifacts/game-asr-20261009/code-recovery-v1/`。
原冻结代码保留，旧计划/profile/checkpoint 不改写。`work/implementation-migration.json` 精确记录代码前后摘要、
兼容性说明和验证证据，正式 execution/manifest 引用该迁移；原模型、前处理、生成参数和成功结果 schema 不变。
自动恢复针对上述临时服务异常，不覆盖机器重启或监督进程被 SIGKILL 的情况。

小时级 agent supervisor 的完整交接与异常处置见 [ASR supervision](asr-unannotated-supervision.md)。
它在当前会话后台检查新增全量任务的进度、意外退出和未知故障，检查结果自动写入
`artifacts/asr-unannotated-20261010/hourly-supervisor/`；与客户端内部的请求重试/60 秒恢复互补。
四个游戏已经完成，相应监督已结束；历史检查记录保留在原 artifact 目录。
该 agent 值守不是系统 cron，不承诺跨会话关闭或机器重启继续运行。

## 已发布的单表版本

2026-10-10 合并发布 `tts-ann-transcription-20261010T111659bjt-01`，schema 为 `qwen-asr-transcription-v2`。
统一读取入口：
`annotations/transcription/tts-ann-transcription-20261010T111659bjt-01/manifest.json`。
每个数据集通过 `outputs[].table` 打开固定 Lance 版本；读取 `sample_id/status/error_code/result`。
总计 1,543,186 行：1,541,304 成功，1,882 失败；失败的 result 为 null。
原始双表版本保留，ASR 未重新计算；新表逐字段回读与原版一致，原 manifest 摘要保持不变。
真实 1,000 条试迁移及正式全量验证在 `artifacts/transcription-single-table-20261010/`。
