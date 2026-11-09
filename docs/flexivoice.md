# FlexiVoice 基础接入

原始目录 `/home/yanglin/FlexiVoice`，目标 `DATA-TTS-UNIFIED/datasets/flexivoice/v0.1`。
使用 `resolved_json` 的 15 个 JSON array 配对本地 `audio`；不使用旧绝对路径读取音频。

每条 JSON 记录对应一个 sample，source_key 为 `<子集>:<零基行号>`，source_config 为 JSON stem。
source-unit-v1 同时固定 JSON 和全部引用音频的相对路径及 SHA256。根目录搬迁不改变身份；
任意依赖内容改变会改变对应来源单元的快照。行号依赖冻结 JSON 顺序。

text 原样保留（空白字符串映射 null）；instruction 原样进入 metadata.instruction，
包括空串/null；完整上游记录同时保存在 metadata.upstream，包含 code_path、duration、meta 等。
code_path 仅为来源追溯，不代表可读 NPZ。实际 duration/rate/channels/frames 来自音频读取器。
音频保持原编码 bytes，重复引用保留独立记录，group_id 绑定原相对音频路径。
不从描述性 instruction 推断 speaker 或语言。speaker 留空；语言仅映射 Emilia 和游戏目录
明确的 EN/ZH/JA/KO 标签，其他来源留空，后续可通过有证据的标注补充。

当前仅发布基础 samples，不自动创建 selection、提取特征或更改训练配方。
既有 selection 去重策略需要单独评审：同音频的 instruction 变体不能静默丢弃，
与现有 Emilia/游戏数据的重复关系也需要基于哈希核实。基础 27 列 schema 不变。
训练构建需要显式消费 metadata.instruction，基础接入本身不代表训练已使用该字段。

```bash
python scripts/tts_data.py bulk flexivoice \
  --root /home/yanglin/FlexiVoice \
  --output /workspace/data/DATA-TTS-UNIFIED/datasets/flexivoice/v0.1 \
  --workers 4 --batch-mib 1 --shard-mib 1024
```

15 个 JSON 各为一个可恢复批次；音频作为外部语义依赖参与来源校验。
standard 验证包括来源哈希、逐条音频头与记录验证、全部 Lance 回读、全局身份核对。
只有最终 manifest 为 complete 才算发布成功。恢复使用同一冻结代码和参数并追加 --resume。

预期 4,199,042 行、4,124,982 个不同音频路径；行级 audio_bytes 会因重复引用而大于
原始唯一音频总字节数 798,181,051,732，不应将这两个计数口径混为一谈。

发布记录：2026-11-08 以上述命令完成，manifest 为 complete，4,199,042 行，约 9,752 小时，676 个分片（standard 验证，未全量解码）。
采样率含 8 kHz 2 条、12 kHz 33 条，下游 selection 需自行过滤。
