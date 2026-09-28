# Lance 全量转换与恢复

13 个已适配来源的全量转换已启动。实时进度以状态脚本为准；启动参数在 reports/current-conversion/launch.json。
固定 pylance 12.0.0、Lance 文件格式 2.2；数据 contract 和 release 均为 v0.1。

## 单一发布表

输出为 `DATA-TTS-UNIFIED/datasets/<dataset_id>/v0.1/{manifest.json,samples.lance/}`。
worker 通过官方 write_fragments 写同一表的数据文件，验证完成后保存外部 checkpoint；
协调者做全局身份对账、统一 commit、sample_id BTREE 索引与 snapshot 计数验证，最后原子发布。
最终数据没有 batch 目录；内部恢复仍可按批记录。

工作状态在 `datasets/<dataset_id>/.state/v0.1/`，包括 plan/status/checkpoints/identity.sqlite。
日志放 pipeline reports。未发布数据位于 v0.1.incomplete，恢复不能直接当成训练数据。

## 命令

```bash
# 示例，选择一个数据集启动；并发应根据实际存储与内存基准设置。
python scripts/tts_data.py bulk libriheavy \
  --root /workspace/data/DATA-TTS/libriheavy \
  --output /workspace/data/DATA-TTS-UNIFIED/datasets/libriheavy/v0.1 \
  --workers 32 --shard-mib 1024 --batch-mib 4096

python scripts/conversion_status.py
```

| dataset 参数 | 原始根目录末级 |
| --- | --- |
| csemotions | CSEMOTIONS |
| libritts_r | LibriTTS-R |
| libriheavy | libriheavy |
| mls_sidon | mls_sidon |

--shard-mib 是 Lance 文件软目标，不再是精确原始 audio bytes 阈值。
--batch-mib 控制调度输入组大小，与样本身份无关。worker 可较大，但各进程还可能产生引擎线程，
不能把 worker 数直接当 CPU 总线程数；多任务共享 CPU/文件系统，必须看端到端吞吐。

## 恢复

同样命令追加 --resume。代码、依赖、来源和影响结果的转换参数保持一致，可调整 worker 数。
校验已完成 fragment 的哈希后复用；不兼容旧 Parquet 检查点。
孤立未提交 fragment 仅在所有 worker 退出、有效检查点全部核验后清理；不在 worker 并行时按 glob 删除文件。
完成发布已存在则拒绝覆盖。想重跑应先明确清理目标，不能静默 overwrite 已发布的表。

## 验证和性能

standard：来源完整哈希、逐记录验证、原 bytes 哈希/音频头、完整 Lance 文件回读、跨批 ID 唯一、索引和 snapshot。
deep：额外全音频解码、有限值/帧数验证，以及来源内容再次哈希。
小规模 fixture、真实来源抽样、全量数据验收是不同证据；性能基准需注明数据范围和冷热缓存。

新标注任务的 schema 变更由单一协调者提交，转换器的并行 fragment 写法不等于可以任意并发增列。

## MLS v0.1 已批准的来源排除

已确认上游英文末包本身截断，用户批准排除整个包；保留原始文件。
MLS 启动及恢复命令增加：

```bash
--exclusions configs/source-exclusions/mls_sidon-v0.1.json
```

计划与发布 manifest 保存 excluded_source_files（路径、字节数、SHA256、原因）。
当前选择 2,627 包，排除 1 包；发布描述必须注明排除，不能称完整覆盖上游全部包。
文件内容变化会拒绝旧排除策略。此机制不自动跳过其他未知坏包。

## 新增来源

CLI 也支持 aishell3、ljspeech、vctk、hifitts、wenetspeech4tts、genshin_voice、starrail_voice、galgame、wutheringwaves。
先用 preview 检查，正式输出仍为 `/workspace/data/DATA-TTS-UNIFIED/datasets/<dataset_id>/v0.1`。
小样本验证后，用户已授权启动这九个来源及原四个来源的全量转换。
具体映射、Wenet 外部清单依赖和 archive 临时空间要求见 [接入表](datasets.md)。

## 本次运行

总 worker 配置上限 288：LibriHeavy/MLS/Galgame 各 64；LibriTTS-R/WenetSpeech4TTS 各 24；
原神/星铁各 16；CSEMOTIONS 8；鸣潮 4；AISHELL-3/LJSpeech/VCTK/HiFiTTS 各 1。
实际活跃 worker 受任务数限制；单一压缩包不伪造可并行的任务。每进程限制 Arrow/BLAS/Rayon 线程。
本次为 standard 验证，1 GiB 目标分片、4 GiB 调度输入组；MLS 使用明确的坏包排除清单。
后台进程日志留在 reports/current-conversion，临时解包空间使用 /tmp/tts-data-pipeline。
正在运行的转换固定代码哈希；修改转换代码后不能直接沿用旧检查点恢复。

## 本次修复后的恢复

Galgame 启动/恢复必须带 `--exclusions configs/source-exclusions/galgame-v0.1.json`，仅排除已核实的单条零帧音频。
各任务实际启动命令和代码快照位置以 reports/current-conversion/launch.json 为准；
仍使用旧代码快照的任务不能直接从修改后的工作源码恢复。
常规 resume 继续严格核对代码；单次已审核的兼容迁移在计划与发布 manifest 中保留完整旧/新版本证据。

## Wenet 缺失转写修复入口

本次恢复固定使用 `reports/runtime/wenet-pairing/scripts/tts_data.py`，
并传入该工作树的 `configs/source-exclusions/wenetspeech4tts-v0.1.json`。
仅排除已完整核实的一条缺失转写，保留其他记录；主工作树源码仍有旧任务使用，保持冻结。
实际命令以 reports/current-conversion/launch.json 为准。
