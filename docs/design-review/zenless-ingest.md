# Zenless samples 接入检查

2026-10-09 检查本地 `/workspace/data/DATA-TTS/zenless-voice` 全部 46 个 Parquet 的元数据，
共 406,720 行，源文件 144,510,824,778 bytes。文件编号连续，行数与本地数据卡一致。
数据卡明确字幕来自游戏，可能描述非语言声音或虚构语言，不是已核验的 ASR 转写。

| 来源语言 | 行数 | 缺文本 | 缺角色 |
| --- | ---: | ---: | ---: |
| Chinese | 102,776 | 31,098 | 19,762 |
| English | 101,430 | 30,890 | 19,676 |
| Japanese | 100,175 | 30,629 | 19,660 |
| Korean | 102,339 | 30,812 | 19,687 |
| 合计 | 406,720 | 123,429 | 78,785 |

四个分散 shard 各抽取每语言四条，共完整解码 64 条：均为单声道 WAV，
59 条 48 kHz、3 条 36 kHz、2 条 32 kHz。检查实际帧数和有限值，未发现异常。
这不是全量音频质量结论，也不据此把全部来源采样率写死为 48 kHz。

## 与其他游戏的一致映射

沿用原神、星铁的 `_game_voice`，注册 `zenless_voice`：

- 原音频 bytes 保留，不转 FLAC、不重采样、不裁剪、不加 padding。
- 来源身份使用固定文件摘要和 shard 原始行号；不以缺失或重复的角色/游戏文件名当主键。
- Chinese/English/Japanese/Korean 映射为 zh/en/ja/ko；角色限定在 dataset 与配音语言内部。
- 空文本、空角色保持未知；保留原字幕、voice_type、ingame_filename、语言与角色原值。
- 性别模板、颜色标签、无台词声音不在 samples 接入时删除或自动修正。
  voice_type=Galgame 是本游戏内部分类，与独立 Galgame 数据集无关。
- 仅发布 samples；不创建 selection、codec、speaker、merged 或 Zenless annotation。

全量任务使用现有 bulk 转换器、16 个 CPU worker、1 GiB 输出软目标与 4 GiB 输入调度组，
开启 deep-verify，检查所有输入哈希、音频解码、输出回读及全局身份后原子发布。
坏记录按通用接入规则报错，不能把抽样通过当作自动忽略坏记录的授权。
源文件 sidecar 摘要保存在检查报告中，完成后可与 manifest 实算输入哈希核对。

目标：`datasets/zenless_voice/v0.1/{manifest.json,samples.lance/}`。
运行日志和冻结代码保存在 `artifacts/zenless-ingest/`；进度以工作状态或发布 manifest 为准，
本文不是任务完成证明。检查报告为同目录 `source-check.json`。
