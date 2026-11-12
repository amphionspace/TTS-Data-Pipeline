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

## 内嵌 CAF 头的 kespeech 音频（2026-11-11）

kespeech 子集有 326,006 条 WAV 的 data 块并非 PCM，而是以一个 4096 字节的 Apple CAF 文件头开头（`caff`、`desc`、`lpcm`、`free`、`data`），
其后才是真正的 16-bit little-endian PCM。任何 WAV 读取器都会把头部解码成约 128 ms 的满幅垃圾采样：开头与约 127 ms 处各一下咔声。
经 250 ms 边缘静音归一化后，咔声落在训练目标的 0.25 s，指令模型生成音频在同一位置出现咔声。其他 14 个子集、NV 五个数据集均为 0。

处理：base 不修改；`selection-flexivoice-v2` 按 103 排除这些音频（证据与规则见 [12](data-contract/specs/12-selections.md)），
features 由全量 dots_vae 复用发布。剥离 CAF 头后重新提取可找回这批数据，尚未执行。

```bash
python scripts/audit_embedded_caf.py --root /workspace/data/DATA-TTS-UNIFIED --dataset flexivoice --out reports/embedded-caf
python scripts/build_selection.py plan --work /tmp/selection-flexivoice-v2-20261111 \
  --rules configs/selections/selection-flexivoice-v2.json
python scripts/build_selection.py run --work /tmp/selection-flexivoice-v2-20261111 --workers 16
PYTHONPATH=src python -m tts_data_pipeline.dots_vae.reselect --root /workspace/data/DATA-TTS-UNIFIED \
  --selection selections/tts-selection-supervised-tts-20261111T162703bjt-01/manifest.json \
  --features datasets/flexivoice/v0.1/features/dots_vae/flexivoice-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-ref3s-20261109T160744bjt-01/manifest.json \
  --work /workspace/workspace/yanglin/work/dots-vae-flexivoice-selected-nocaf-20261111
```

结果：selection `tts-selection-supervised-tts-20261111T162703bjt-01` 入选 **1,330,267 行 / 2,214.4 原生小时**（v1 为 1,656,126 行）；
features `flexivoice-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-ref3s-20261111T164240bjt-01` 于 2026-11-11 17:05 发布，全量校验通过。

## v3：去掉 250 ms 边缘静音、修复 CAF 头（2026-11-12）

v2 特征沿用 dots 训练预处理的 250 ms 边缘静音归一化：首个超过（峰值 −30 dB）阈值的采样前补零或裁剪到恰好 250 ms。
从第一个采样就开始发声的录音（FlexiVoice train 4.45%、NV 0.1–9.8%）因此在 0.25 s 处从数字零阶跃到语音，所有起音也被对齐到 0.25 s；
去 CAF 头的 2 epoch 实验生成音频仍有开头瞬态。v3 改为 dots_vae waveform 策略 `raw-caf-repair-v1`（profile `dots-tts-soar-vae-xvec-48k-fp32-v3-ref3s`）：

- 解码前用 `tts_data_pipeline/wav_repair.py` 检查 WAV data 块是否以 `caff` 开头；是则核对 CAF desc 为 16-bit little-endian 整数 PCM、
  采样率和声道与 WAV 头一致，跳过头部（kespeech 均为 2048 帧），特征行的 start_frame 记跳过的帧数。不满足条件即报错，不猜测。
- 不做边缘静音归一化（edge_leading/trailing_delta 均为 0），保留录音本身的首尾；只在末尾补零到 7680 的整数倍。
- 参考段规则、x-vector、VAE 与 v2-ref3s 相同。

selection `tts-selection-supervised-tts-20261112T112338bjt-01`（selection-flexivoice-v3）按 v3 波形时长重算参考池，KeSpeech CAF 样本全部保留，入选 1,655,877 行。
NV 五个数据集同 profile 的特征为 `<dataset_id>-…-v3-ref3s-20261112T121520bjt-01`；训练 build 要求所有 binding 同一 profile_id。

```bash
export TTS_DOTS_SOURCE=$PWD/cache/third_party/dots.tts PYTHONPATH=src
python scripts/build_selection.py plan --work /tmp/selection-flexivoice-v3-20261112 --rules configs/selections/selection-flexivoice-v3.json
python scripts/build_selection.py run --work /tmp/selection-flexivoice-v3-20261112 --workers 16
python scripts/extract_dots_vae.py plan-selection --selection <selection manifest> --work <work> \
  --model <dots.tts-soar> --reference-rule dots-reference-v2:min-3s-on-patches --waveform-policy raw-caf-repair-v1
python scripts/extract_dots_vae.py run --work <work> --gpus 0 1 2 3 4 --workers-per-gpu 2 --cpu-threads 6
```

