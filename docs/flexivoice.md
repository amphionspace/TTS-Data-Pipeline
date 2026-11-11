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

基础接入只发布 samples；selection、文本修订与特征发布另行执行，见下方流程。
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


## 指令训练的数据决策（2026-11-10）

分工与副语言数据相同：text revision annotation → selection → features → training build。

- `flexivoice_text` / `flexivoice-text-v1`：移除 `[inaudible]`，将 `[LAUGHTER]`、`[BREATHING]` 改为现有 NV 标签拼写；含 `[GARBAGE]`/`[garbage]` 的不完整转写和未知事件标签标为 skipped。修订记录与原文同时保留，selection 以 1003 排除不可用修订。
- `configs/selections/selection-flexivoice-v1.json`：固定 11 个子集、1–30 秒、≥16 kHz、66 个留出说话人键；缺失语言允许，音频的指令变体保留，冲突重复保守排除。跨句参考使用闭合的入选集合，≥3 秒判定绑定全量 dots_vae 的实际处理时长（而非原生时长），具体原因码见规范第 12 章 version 8。
- dots_vae 从已发布的全量特征重新选择：固定 selection branch/version 和文本来源；核对 sample_id、音频哈希、原生采样率/区间及 profile 后复用 latent/x-vector。新表只含入选目标并携带 selected_text，不覆盖全量特征，不再次使用 GPU。发布前全量校验 payload 哈希、目标覆盖和文本一致性。
- 训练 build 只消费新 features、指令和说话人元数据；不再做文本清理或子集/时长/采样率/留出/配对筛选。配对可用性保留为不变量校验，失败则拒绝 build。

文本修订发布：`tts-ann-flexivoice_text-20261110T091454bjt-01`，4,199,042 行，4,199,029 ok、13 skipped（不完整转写）。2 处 inaudible 移除、34 处笑声和 9 处呼吸映射；642,398 行有修订（大部分是首尾空白）。

```bash
PYTHONPATH=src python -m tts_data_pipeline.annotations.flexivoice_text \
  --root /workspace/data/DATA-TTS-UNIFIED --work /workspace/workspace/yanglin/work/flexivoice-text-20261110
python scripts/build_selection.py plan --work /tmp/selection-flexivoice-20261110 \
  --rules configs/selections/selection-flexivoice-v1.json
python scripts/build_selection.py run --work /tmp/selection-flexivoice-20261110 --workers 8
# 选定固定的 selection 和已有 features manifest 后：
PYTHONPATH=src python -m tts_data_pipeline.dots_vae.reselect \
  --root /workspace/data/DATA-TTS-UNIFIED --selection <selection-manifest> \
  --features <all-samples-dots-vae-manifest> --work <new-work-directory>
```

所有命令创建新产物；示例的工作目录已使用，不能原样重复发布。


全量时长复核：第一份 selection `tts-selection-supervised-tts-20261110T091733bjt-01` 使用原生时长判断参考，保留 1,655,877 行，但按 dots 实际处理时长检查有 657 行无法维持闭合参考池，因此该版本不用于训练，未基于它发布 features。后续发布固定 `reference_duration_input`，不覆盖或改写第一份发布。

最终 selection：`tts-selection-supervised-tts-20261110T092455bjt-01`，入选 **1,656,126 行 / 2,742.4787 原生小时**；dots 处理后总时长 2,758.1938 小时。再次全量检查参考池，未发现无参考目标（0 行）。对应规则即本仓库 `selection-flexivoice-v1.json`，发布时固定了全量特征作为处理后时长证据；该发布替代上面的原生参考时长试版。

对应 selection features 已于 2026-11-10 09:59（北京时间）发布：`flexivoice-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-ref3s-20261110T093409bjt-01`，1,656,126 行，全部 payload 哈希、目标覆盖和最终文本回读校验通过。manifest 位于 `datasets/flexivoice/v0.1/features/dots_vae/<feature_id>/manifest.json`。
