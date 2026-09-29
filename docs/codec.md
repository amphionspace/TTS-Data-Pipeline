# Codec C 执行

首轮固定 selection `tts-selection-supervised-tts-20260929T151539bjt-01` 的 `selection_reason=0`：
16 个数据集，128,220,178 条，约 357,506.84 小时。原始音频和 selection 不重写。

全量 C 已于 **2026-09-29 18:25:52（北京时间）** 启动。
运行目录为 `artifacts/codec-runs/qwen3-codec-c-20260929T182519bjt-01/`；使用 8 卡 × 每卡 2 进程 × 每进程 4 解码线程。
A 已停止，B 未启动；A/B 的结果、检查点、运行副本和过时试验工具已退役。
不要使用旧 A/B 的 run 路径恢复，也不能将其 codes 作为 C 的结果复用。

## 固定实现

- `src/tts_data_pipeline/codec.py`：波形处理、身份与 profile、模型加载、C 入口。
- `src/tts_data_pipeline/codec_batch.py`：固定 batch、有效长度、FA2 全因果窗口与 FP32 码本缓存。
- `src/tts_data_pipeline/codec_fast.py`：避免 GPU 标量同步的整数 padding 计算。
- `src/tts_data_pipeline/codec_run.py`：按长度桶调度、检查点、验证和发布。
- `src/tts_data_pipeline/codec_text.py`：发布最终选用文本与语言。

所有修改位于本仓库；不修改 `site-packages`，不将本项目安装为 distribution。
本次已复制仓库源码为固定 runtime，文件摘要记录在运行目录的 `runtime-files.json`。
第三方源码、依赖版本及模型权重也固定摘要。

profile 名为 `qwen3-12hz-24k-k16-fp16-fa2-canonical-v1`，当前实现 ID：
`917f86ce31fed41976e6a065b0a3ecc6826337006268370109be80984fbcee67`。
模型为冻结的 Qwen3-TTS-Tokenizer-12Hz；24 kHz、16 码本、每码本 2048、int16 `[T,16]`。
FP16 卷积/Transformer，FA2 全因果注意力，FP32 码本缓存与量化距离计算。
2 秒长度桶，固定批大小最大 64、padding 后音频预算 480 秒；不是根据当前空闲显存动态改 batch。

## 已完成的端到端对照

AISHELL-3、LJSpeech、CSEMOTIONS 入选全集共 105,273 条、119.7314 小时。
两轮输出的 codes hash 与全部选用文本/语言/文本版本一致，并独立回查固定 selection。

| 每卡进程 / 解码线程 | 编码阶段（含启动与 IO） | 完整校验发布 |
| --- | ---: | ---: |
| 1 / 8 | 84.94 秒 | 95.19 秒 |
| 2 / 4 | 66.56 秒 | 78.98 秒 |

正式任务采用 8 卡 × 每卡 2 进程 × 4 解码线程。此次为共享 GPU 上的小数据集对照，
不能直接作为 35.75 万小时全库的工期保证；全量启动后需按实际 checkpoint 窗口观察。
临时试跑数据已清理，保留比较结果、各数据集 manifest 副本及验收摘要。

## 验证与启动

验收目录：`artifacts/codec-validation/c-fp16-fa2/`。
包含八卡码字/边界验证、profile、端到端吞吐对照、清理清单；用户试听目录为
`artifacts/codec-listening/c-fp16-fa2-20260929T1804bjt/`。

一次性样本准备和验收脚本已清理；抽样、边界、跨卡与混批核验步骤见
[验证方法](design-review/codec-inference-validation.md)。保留验收 JSON、试听结果摘要与固定目标清单；试听音频、复制的 fixture 与实验 speaker 张量已清理。
这些记录证明本次 profile 的验收范围，不能代替未来修改实现后的重新验收。


本次固定的 plan、preflight、runtime 和 launch 记录均在 `artifacts/codec-runs/qwen3-codec-c-20260929T182519bjt-01/`。
`reports/codec/active.json` 保存 PID、完整命令和源码/计划摘要；日志为 `reports/codec/production-c.log`。
恢复前先确认旧协调器及其 worker 已退出，再执行原冻结源码：

```bash
/home/yanglin/miniforge3/envs/tts-features/bin/python \
  artifacts/codec-runs/qwen3-codec-c-20260929T182519bjt-01/runtime-readable-v1/scripts/extract_codec.py run \
  --work artifacts/codec-runs/qwen3-codec-c-20260929T182519bjt-01 \
  --gpus 0 1 2 3 4 5 6 7 --workers-per-gpu 2 --decode-threads 4
```

固定任务成员沿用已核对的 selection 目标清单，不重新扫描音频；每个 worker 仍验证任务的
有序 sample_id 摘要、原始音频哈希和时间轴。复用的只有目标清单，不复用 A/B 的 codes。

最终输出位于 `/workspace/data/DATA-TTS-UNIFIED/datasets/<dataset>/v0.1/features/codec/<run_id>/`。
未发布输出使用 `.incomplete`，checkpoint 在该 dataset 的 `.state/v0.1/features/codec/<run_id>/`。
2026-09-29 18:36（北京时间）移除多余的 profile 哈希目录层，保留 310 个检查点、1,072,750 条结果并续跑。
profile 哈希仍存在于 manifest/行中；迁移清单位于运行目录的 `migrations/readable-run-layout-v1/`。
每个 dataset 对外发布一张 `features.lance`，不会按 GPU/batch 发布多张表。
编码完成后协调器校验覆盖、追加选用文本、建立索引并发布；编码完成不等于已经发布。

## 恢复与保留

同一 C profile 通过原 frozen runtime 恢复；逐个文件核对已完成 checkpoint 再跳过。
OOM 或数值错误时停止，不通过减小 canonical batch 或静默丢弃目标继续运行。
修改数值实现须创建并验收新 profile；并发和预取配置变化也需要核对结果一致性。
运行目录、验收 evidence、模型 cache、固定快照与当前 `.incomplete/.state` 是活动依赖。

当前仅 codec。最终表包含 text、language、文本版本、来源和说话人元数据；
speaker embedding 另行验收，后续通过训练 build 分支绑定 locator。
约定见 [06](data-contract/specs/06-codecs.md)，技术证据见 [验证记录](design-review/codec-inference-validation.md)。
