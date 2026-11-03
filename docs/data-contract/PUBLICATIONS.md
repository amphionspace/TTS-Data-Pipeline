# 正式发布登记

统一根 `/workspace/data/DATA-TTS-UNIFIED` 的正式发布与维护记录：现在有哪些可用的发布，以及每一次发布、修正、删除、清理做了什么、为什么。

## 本文件格式

1. **当前发布总览**：当前有效的全部正式发布（`status=complete` 的 manifest），一行一个；Features 按 run 后缀一行，覆盖多个数据集。
   每次发布或删除后同步修改此表，它只反映现状。
2. **按类别的事件记录**：基础数据集、Annotation、Selection、Features、统一根维护五节。每个事件一个小节，追加在所属类别末尾，
   同类内按时间从旧到新；已写的小节不修改，后续变化（修正、删除、替代）写成新的小节并引用原 ID。
3. 小节标题为 `### YYYY-MM-DD · 事件 · 对象`，事件取 发布、修正、删除、清理；日期为北京时间。字段：
   - 发布：时间、manifest（相对统一根）、SHA256（manifest 原始字节摘要）、内容（规模、规则或 schema、输入），可选说明。
   - 修正：时间、原因、处理、原/新 SHA256、证据位置。
   - 删除：原因、替代、前提（确认无引用）。
   - 清理：内容、核验、证据。
4. 数值取自 manifest。未完成的运行和工作状态不登记，也不留在统一根下。
5. 本文件随数据契约同步到统一根，修改后运行 `scripts/check_contract.py --target <统一根>` 核对部署副本。
   本登记建于 2026-10-12；此前的事件按现存 manifest 与 pipeline 记录补录，已删除且未留摘要的 manifest 注明“未保留摘要”。

规则版本含义见 [12 · 用途规则版本](specs/12-selections.md#用途规则版本)，manifest 字段要求见 [10](specs/10-manifests.md)。

## 当前发布总览

| 类别 | ID | 内容 | 完成（北京时间） | 用途 |
| --- | --- | --- | --- | --- |
| 基础数据集 | aishell3/v0.1 | 88,035 条，85.6 小时 | 2026-09-28 18:58 | |
| 基础数据集 | csemotions/v0.1 | 4,160 条，10.2 小时 | 2026-09-28 15:58 | |
| 基础数据集 | disfluencyspeech/v0.1 | 5,000 条，9.5 小时 | 2026-11-03 14:52 | |
| 基础数据集 | emilia/v0.1 | 40,264,231 条，101,655.6 小时 | 2026-09-28 23:17 | |
| 基础数据集 | emilia2/v0.1 | 99,840,397 条，146,325.0 小时 | 2026-10-12 03:10 | |
| 基础数据集 | emilia_nv/v0.1 | 174,179 条，573.4 小时 | 2026-11-02 16:04 | |
| 基础数据集 | emilia_yodas/v0.1 | 43,964,905 条，113,820.6 小时 | 2026-09-29 10:30 | |
| 基础数据集 | galgame/v0.1 | 7,101,489 条，10,173.7 小时 | 2026-09-28 18:43 | |
| 基础数据集 | genshin_voice/v0.1 | 654,252 条，1,039.7 小时 | 2026-09-28 19:00 | |
| 基础数据集 | hifitts/v0.1 | 323,978 条，291.7 小时 | 2026-09-29 00:40 | |
| 基础数据集 | hifitts2/v0.1 | 12,809,875 条，35,932.7 小时 | 2026-09-29 11:22 | |
| 基础数据集 | libriheavy/v0.1 | 12,441,758 条，51,044.7 小时 | 2026-09-29 10:27 | |
| 基础数据集 | libritts_r/v0.1 | 374,999 条，583.1 小时 | 2026-09-28 17:39 | |
| 基础数据集 | ljspeech/v0.1 | 13,100 条，23.9 小时 | 2026-09-28 16:44 | |
| 基础数据集 | mls_sidon/v0.1 | 12,288,862 条，50,834.3 小时 | 2026-09-29 04:23 | |
| 基础数据集 | nonverbalspeech38k/v0.1 | 38,718 条，130.6 小时 | 2026-11-03 14:59 | |
| 基础数据集 | nonverbaltts/v0.1 | 6,256 条，17.6 小时 | 2026-11-03 14:57 | |
| 基础数据集 | smiip_nv/v0.1 | 17,377 条，33.1 小时 | 2026-11-03 15:26 | |
| 基础数据集 | starrail_voice/v0.1 | 403,437 条，692.6 小时 | 2026-09-28 19:00 | |
| 基础数据集 | vctk/v0.1 | 88,328 条，82.7 小时 | 2026-09-28 17:46 | |
| 基础数据集 | wenetspeech4tts/v0.1 | 3,932,472 条，7,232.2 小时 | 2026-09-28 20:10 | |
| 基础数据集 | wutheringwaves/v0.1 | 78,777 条，134.8 小时 | 2026-09-28 17:45 | |
| 基础数据集 | zenless_voice/v0.1 | 406,720 条，554.4 小时 | 2026-10-09 18:53 | |
| Annotation | `tts-ann-transcription-20261010T111659bjt-01` | transcription；genshin_voice、starrail_voice、wutheringwaves、zenless_voice；1,543,186 行 | 2026-10-10 11:17 | selection-v2 文本来源 |
| Annotation | `tts-ann-transcription-20261012T092945bjt-01` | transcription；emilia；40,264,231 行 | 2026-10-12 09:39 | selection-v2 文本来源 |
| Annotation | `tts-ann-transcription-20261010T161238bjt-01` | transcription；emilia_yodas；43,964,905 行（ok 43,964,455，failed 450）；vLLM + sglang 两段 | 2026-10-18 09:26 | |
| Annotation | `tts-ann-speaker_structure-20261019T093525bjt-01` | speaker_structure（moss-transcribe-diarize-v1）；emilia；40,264,231 行（ok 40,220,982，failed 43,249） | 2026-10-19 09:58 | |
| Annotation | `tts-ann-transcription-20261019T143130bjt-01` | transcription；hifitts2；12,809,875 行，全部 ok；sglang-omni | 2026-10-20 11:05 | |
| Annotation | `tts-ann-upstream_quality-20261013T143341bjt-01` | upstream_quality（upstream-dnsmos-p835-ovrl-v1）；emilia、emilia_yodas、emilia2；184,069,533 行 | 2026-10-13 16:38 | |
| Annotation | `tts-ann-effective_bandwidth-20261020T153104bjt-01` | effective_bandwidth（welch-band-edge-v1）；emilia；40,264,231 行 | 2026-10-21 10:36 | 带宽测量，不作质量判定 |
| Annotation | `tts-ann-effective_bandwidth-20261020T155412bjt-01` | effective_bandwidth（welch-band-edge-v1）；emilia_yodas；43,964,905 行 | 2026-10-21 10:50 | 带宽测量，不作质量判定 |
| Annotation | `tts-ann-speaker_structure-20261021T124643bjt-01` | speaker_structure（moss-transcribe-diarize-v1）；genshin_voice、starrail_voice、wutheringwaves、zenless_voice；1,543,186 行（ok 1,537,846，failed 5,340） | 2026-10-22 09:23 | |
| Annotation | `tts-ann-speaker_structure-20261019T093525bjt-02` | speaker_structure（moss-transcribe-diarize-v1）；emilia_yodas；43,964,905 行（ok 43,930,224，failed 34,681） | 2026-10-23 12:11 | |
| Annotation | `tts-ann-nv_tag_text-20261103T171702bjt-01` | nv_tag_text（nv-canonical-v1，text revision）；emilia_nv、smiip_nv、nonverbalspeech38k、nonverbaltts、disfluencyspeech；241,530 行（ok 241,235，skipped 295） | 2026-11-03 17:17 | selection-nv-v1 文本来源 |
| Selection | `tts-selection-supervised-tts-20260929T151539bjt-01` | selection-v1；16 个数据集；入选 128,220,178 条，357,506.8 小时 | 2026-09-29 15:39 | 现有训练 build 使用 |
| Selection | `tts-selection-supervised-tts-20261012T123927bjt-01` | selection-v2；18 个数据集；入选 203,998,904 条，477,595.7 小时；上游测试集留出 | 2026-10-12 13:31 | 当前一阶段预训练 |
| Selection | `tts-selection-supervised-tts-20261019T103416bjt-01` | selection-v4；emilia；入选 33,992,131 条，84,158.5 小时；selection-v2 规则 + MOSS 单说话人 | 2026-10-19 11:02 | 训练（emilia 部分） |
| Selection | `tts-selection-supervised-tts-20261103T173114bjt-01` | selection-nv-v1；5 个数据集；入选 230,083 条，730.8 小时；nv-canonical-v1 文本，test/spk_test 留出 | 2026-11-03 17:31 | dots.tts 副语言训练 |
| Selection | `tts-selection-supervised-tts-20261103T190044bjt-01` | selection-game-zhen-v1；4 个游戏数据集，仅 en/zh；入选 680,109 条，1,090.6 小时 | 2026-11-03 19:01 | dots.tts NV 实验的通用数据 |
| Features | text · `text-selection-text-v1-20260930T153654bjt-01` | 16 个数据集，128,220,178 行 | 2026-09-30 17:01 | selection-v1 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-packed-v1-20260930T130829bjt-01` | 16 个数据集，128,220,178 行 | 2026-10-01 05:34 | selection-v1 |
| Features | codec · `codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20260930T181155bjt-01` | 12 个数据集，49,918,118 行 | 2026-10-01 13:33 | selection-v1 |
| Features | codec · `codec-qwen3-12hz-24k-k16-fp32-packed-v1-20260930T024146bjt-01` | 4 个数据集，78,302,060 行 | 2026-10-01 14:56 | selection-v1 |
| Features | merged · `merged-selected-features-v1-20261001T161244bjt-01` | 16 个数据集，128,220,178 行 | 2026-10-01 18:09 | selection-v1 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261008T131631bjt-01` | 16 个数据集，128,220,178 行 | 2026-10-09 00:01 | selection-v1 |
| Features | merged · `merged-reference-features-v1-20261009T000157bjt-01` | 16 个数据集，128,094,617 行 | 2026-10-09 02:32 | selection-v1 |
| Features | codec · `codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261012T134948bjt-01` | 18 个数据集，203,998,904 行 | 2026-10-13 02:50 | selection-v2 |
| Features | text · `text-selection-text-v1-20261012T140653bjt-01` | 18 个数据集，203,998,904 行 | 2026-10-12 18:31 | selection-v2 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261012T193900bjt-01` | 17 个数据集，125,601,114 行 | 2026-10-13 00:39 | selection-v2 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261013T025148bjt-01` | 1 个数据集（emilia2），78,397,790 行 | 2026-10-13 06:35 | selection-v2 |
| Features | merged · `merged-reference-features-v1-20261013T004007bjt-01` | 17 个数据集，125,485,003 行 | 2026-10-13 01:24 | selection-v2 |
| Features | merged · `merged-reference-features-v1-20261013T063612bjt-01` | 1 个数据集（emilia2），76,684,408 行 | 2026-10-13 07:00 | selection-v2 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261016T103356bjt-01` | 1 个数据集（emilia2），78,397,790 行；prefix reference | 2026-10-16 14:20 | selection-v2（另一 pretrain） |
| Features | merged · `merged-prefix-reference-features-v1-20261016T142123bjt-01` | 1 个数据集（emilia2），76,684,408 行；prefix reference | 2026-10-16 14:43 | selection-v2（另一 pretrain） |
| Features | codec · `codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261019T120241bjt-01` | 1 个数据集（emilia），33,992,131 行 | 2026-10-19 13:10 | selection-v4 |
| Features | text · `text-selection-text-v1-20261019T120740bjt-01` | 1 个数据集（emilia），33,992,131 行 | 2026-10-19 13:51 | selection-v4 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261019T131032bjt-01` | 1 个数据集（emilia），33,992,131 行 | 2026-10-19 14:35 | selection-v4 |
| Features | merged · `merged-reference-features-v1-20261019T143526bjt-01` | 1 个数据集（emilia），33,992,131 行 | 2026-10-19 14:51 | selection-v4 |
| Features | speaker_embedding · `speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261019T150157bjt-01` | 2 个数据集（emilia、emilia_yodas），75,844,468 行；prefix reference | 2026-10-19 17:41 | selection-v2（prefix） |
| Features | merged · `merged-prefix-reference-features-v1-20261019T174217bjt-01` | 2 个数据集（emilia、emilia_yodas），75,844,468 行；prefix reference | 2026-10-19 18:01 | selection-v2（prefix） |
| Features | dots_vae · `emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T095118bjt-01` | 1 个数据集（emilia_nv），174,179 行全部 ok；all_samples；dots.tts-soar VAE posterior + CAM++ x-vector + 参考段 x-vector（dots-reference-v1:codec-grid-v1-on-patches） | 2026-11-03 11:25 | dots.tts 副语言微调 |
| Features | dots_vae · `<dataset_id>-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T173207bjt-01` | 5 个数据集，230,083 行全部 ok；selection_branch（selection-nv-v1），text_kind=nv-canonical-v1 | 2026-11-03 18:34 | dots.tts 副语言训练（nv26 分词器） |
| Features | dots_vae · `<dataset_id>-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T190254bjt-01` | 4 个游戏数据集，680,109 行全部 ok；selection_branch（selection-game-zhen-v1），text_kind=asr_transcription | 2026-11-03 22:46 | dots.tts NV 实验的通用数据 |

## 基础数据集

### 2026-09-28 · 发布 · csemotions/v0.1

- 时间：2026-09-28 15:58（北京时间）
- manifest：`datasets/csemotions/v0.1/manifest.json`
- SHA256：`4ad2a12757477367ea2052bd7240480d86d65e488ccfc5e4f5a36661564dcbb7`
- 内容：4,160 条，10.2 小时

### 2026-09-28 · 发布 · ljspeech/v0.1

- 时间：2026-09-28 16:44（北京时间）
- manifest：`datasets/ljspeech/v0.1/manifest.json`
- SHA256：`602371f887b1399f7a59a147b52cb6929091458819670656f5935783ede0c4d4`
- 内容：13,100 条，23.9 小时

### 2026-09-28 · 发布 · libritts_r/v0.1

- 时间：2026-09-28 17:39（北京时间）
- manifest：`datasets/libritts_r/v0.1/manifest.json`
- SHA256：`195ae41b0cfec40038c1bb2dda7c7d376773cabe803c7c5c66f940a0dab67799`
- 内容：374,999 条，583.1 小时

### 2026-09-28 · 发布 · wutheringwaves/v0.1

- 时间：2026-09-28 17:45（北京时间）
- manifest：`datasets/wutheringwaves/v0.1/manifest.json`
- SHA256：`703cd0abca6b32cc1de7a5bfe7eea6bfe000613eec44437818a392c59bde2fc8`
- 内容：78,777 条，134.8 小时

### 2026-09-28 · 发布 · vctk/v0.1

- 时间：2026-09-28 17:46（北京时间）
- manifest：`datasets/vctk/v0.1/manifest.json`
- SHA256：`08ec20fa37ed435eb2f5b324c375aad9a9f415eeb39f0ef5845528e060d4f386`
- 内容：88,328 条，82.7 小时

### 2026-09-28 · 发布 · galgame/v0.1

- 时间：2026-09-28 18:43（北京时间）
- manifest：`datasets/galgame/v0.1/manifest.json`
- SHA256：`3fb044f166086c2bef9ee23e975f2f54f987bd8dcff24e59a8af2a765e7ea624`
- 内容：7,101,489 条，10,173.7 小时

### 2026-09-28 · 发布 · aishell3/v0.1

- 时间：2026-09-28 18:58（北京时间）
- manifest：`datasets/aishell3/v0.1/manifest.json`
- SHA256：`e76f13885ea2c64bc2d578f8905ee263086889a402022df7f183f304b8cd9e72`
- 内容：88,035 条，85.6 小时

### 2026-09-28 · 发布 · starrail_voice/v0.1

- 时间：2026-09-28 19:00（北京时间）
- manifest：`datasets/starrail_voice/v0.1/manifest.json`
- SHA256：`02f86a4231090fb277bc917101ead70aa3d78618c001dd88a089bd0ceb621411`
- 内容：403,437 条，692.6 小时

### 2026-09-28 · 发布 · genshin_voice/v0.1

- 时间：2026-09-28 19:00（北京时间）
- manifest：`datasets/genshin_voice/v0.1/manifest.json`
- SHA256：`77b812e62d868c887e73a5731ff10c119159a93a8897a6292dcf2d1157f9abf4`
- 内容：654,252 条，1,039.7 小时

### 2026-09-28 · 发布 · wenetspeech4tts/v0.1

- 时间：2026-09-28 20:10（北京时间）
- manifest：`datasets/wenetspeech4tts/v0.1/manifest.json`
- SHA256：`cfab09cbcaea96e2d41703348aa50e4729a214294682c1f520b47b5beb6eb786`
- 内容：3,932,472 条，7,232.2 小时

### 2026-09-28 · 发布 · emilia/v0.1

- 时间：2026-09-28 23:17（北京时间）
- manifest：`datasets/emilia/v0.1/manifest.json`
- SHA256：`0d973d59af4cede097b84a1d13c917033ded879124ac1b05eaa23d58672ed1ba`
- 内容：40,264,231 条，101,655.6 小时

### 2026-09-29 · 发布 · hifitts/v0.1

- 时间：2026-09-29 00:40（北京时间）
- manifest：`datasets/hifitts/v0.1/manifest.json`
- SHA256：`d784e3c9964ad9c08a4fb9ede4545ed506e639f33f56f8605c90ce7395f3f98a`
- 内容：323,978 条，291.7 小时

### 2026-09-29 · 发布 · mls_sidon/v0.1

- 时间：2026-09-29 04:23（北京时间）
- manifest：`datasets/mls_sidon/v0.1/manifest.json`
- SHA256：`8250fed86f018f630ed02e86ef3c4448e96fddd5dde62040f2738e0c51ab1201`
- 内容：12,288,862 条，50,834.3 小时

### 2026-09-29 · 发布 · libriheavy/v0.1

- 时间：2026-09-29 10:27（北京时间）
- manifest：`datasets/libriheavy/v0.1/manifest.json`
- SHA256：`2351f302dbe09c0ca2713d8c592962e6a0c69f48d6694b599b10f9fe0c57130f`
- 内容：12,441,758 条，51,044.7 小时

### 2026-09-29 · 发布 · emilia_yodas/v0.1

- 时间：2026-09-29 10:30（北京时间）
- manifest：`datasets/emilia_yodas/v0.1/manifest.json`
- SHA256：`af27299a5db4616a80efe869c44e52481cae7c9eda94d8727b0e71bdc78e83ee`
- 内容：43,964,905 条，113,820.6 小时

### 2026-09-29 · 发布 · hifitts2/v0.1

- 时间：2026-09-29 11:22（北京时间）
- manifest：`datasets/hifitts2/v0.1/manifest.json`
- SHA256：`237178b0f9f3300565ee1f2f68393d78849002bad0f0fcfd995647b18403ba4c`
- 内容：12,809,875 条，35,932.7 小时

### 2026-10-09 · 发布 · zenless_voice/v0.1

- 时间：2026-10-09 18:53（北京时间）
- manifest：`datasets/zenless_voice/v0.1/manifest.json`
- SHA256：`6e6317e5caa55d25e7ec809dc2a4c68ba934e5d6cdeb927d293206e2cd83d8b6`
- 内容：406,720 条，554.4 小时

### 2026-10-12 · 发布 · emilia2/v0.1

- 时间：2026-10-12 03:10（北京时间）
- manifest：`datasets/emilia2/v0.1/manifest.json`
- SHA256：`（发布时原值见同日修正记录）`
- 内容：99,840,397 条，146,325.0 小时

### 2026-10-12 · 修正 · emilia2/v0.1 manifest

- 时间：2026-10-12（北京时间）
- 原因：manifest 缺 spec 10 的 finished_at、code_sha256，以及 speakers、excluded_source_files、excluded_source_records、checkpoint_code_versions、code_migration
- 处理：在任何下游使用前按发布与运行目录中的证据补全字段，samples 数据与 Lance 版本不变；finished_at 取原 manifest 写入时间；code_migration 记录 7,108 个归档由一次性脚本从 FLAC24 原位重编码；修正过程写入 manifest_correction
- SHA256：原 `24779fb04467d1926d5ed7e90b310cffd1101059de6406c9eb6fbe424a1102c7` → 新 `873d584c19d70681c7fdb9af5c032259f1f0eccc9629382b81531a3a29849120`
- 同时：接入证据 ingestion-candidates、ingestion-checkpoints、candidate-exclusions.jsonl 移出发布目录，发布目录与其他数据集一致
- 证据：pipeline 仓库 `artifacts/emilia2-runs/emilia2-flac16-20261010/`（manifest.original.json、fix_manifest.py、release-evidence/）

### 2026-11-02 · 发布 · emilia_nv/v0.1

- 时间：2026-11-02 16:04（北京时间）
- manifest：`datasets/emilia_nv/v0.1/manifest.json`
- SHA256：`b0eecbf677507ad4e60674ff7ef2421dcccda74655c8db8e56b7f6476efe6be8`
- 内容：174,179 条，573.4 小时；HF amphion/Emilia-NV 全部 18 个 webdataset tar，zh；text 为含内联副语言标签的上游 ASR 文本（text_kind=source_asr_paralinguistic），无 speaker
- 说明：deep 验证，174,179 条全部完整解码；libmpg123 对部分 MP3 帧报告 part2_3_length 警告，不影响解码结果

### 2026-11-03 · 发布 · disfluencyspeech/v0.1

- 时间：2026-11-03 14:52（北京时间）
- manifest：`datasets/disfluencyspeech/v0.1/manifest.json`
- SHA256：`b4cca0ca06bf4380ad7714756a87524f6b99d12c9d606e12eab9b3dd34969828`
- 内容：5,000 条，9.5 小时；HF amaai-lab/DisfluencySpeech（Apache-2.0）train/validation/test，单说话人，en；
  text 为含 Switchboard 不流利标注与事件标签的标注转写（source_transcript_annotated），三种删减转写为变体
- 说明：deep 验证；原 split 存 metadata，test 250 条由 selection 留出

### 2026-11-03 · 发布 · nonverbaltts/v0.1

- 时间：2026-11-03 14:57（北京时间）
- manifest：`datasets/nonverbaltts/v0.1/manifest.json`
- SHA256：`c5061d0ea4f96cafeb9cd7e1b50112da8cddb4e4218ac28e7deaf9e157f3f9fe`
- 内容：6,256 条，17.6 小时；HF deepvk/NonverbalTTS（Apache-2.0）train/dev/test/other，en（VoxCeleb、Expresso）；
  text 为三人标注融合文本（emoji 非语言标签，source_transcript_paralinguistic），ASR 初稿为变体，speaker 来自上游
- 说明：deep 验证；原 split 存 metadata

### 2026-11-03 · 发布 · nonverbalspeech38k/v0.1

- 时间：2026-11-03 14:59（北京时间）
- manifest：`datasets/nonverbalspeech38k/v0.1/manifest.json`
- SHA256：`1c47e099d778ad492b44a2d3185a7788329048001d97a4fe84c2afa884e6e4d0`
- 内容：38,718 条，130.6 小时；HF nonverbalspeech/nonverbalspeech38k（CC BY-NC 4.0）全部 46 片；
  zh 36,925 条 / 126.4 小时，en 1,793 条 / 4.2 小时；text 为 08-31 版 caption（每条一个 `[tag]`，source_asr_paralinguistic），v1_caption 为变体；无 speaker
- 说明：deep 验证；上游无样本 ID，身份为 shard+row

### 2026-11-03 · 发布 · smiip_nv/v0.1

- 时间：2026-11-03 15:26（北京时间）
- manifest：`datasets/smiip_nv/v0.1/manifest.json`
- SHA256：`0156e010c86961666e9212cd0d829fc2030d2ba2da0f5eca2ceaab497c1bb932`
- 内容：17,377 条，33.1 小时；HF xunyi/SMIIP-NV（CC BY-NC-SA 4.0，gated）SMIIP_NV.zip，37 位说话人，zh，5 类情感；
  text 为点式标注（`<laughter>/<cough>/<crying>`，source_transcript_paralinguistic），区间式标注为变体，全部标注与时间区间存 metadata；3 条无标注
- 说明：deep 验证；SMIIP_NV_SPK_finetune.zip（同一批音频，CRC 一致）作为语义依赖，只提供 spk_train/spk_test，spk_test 2,607 条由 selection 留出

## Annotation

### 2026-10-10 · 发布 · tts-ann-transcription-20261009T191644bjt-01

- 时间：2026-10-10 10:51（北京时间）
- manifest：已删除，未保留摘要
- 内容：transcription，qwen-asr-transcription-v1 双表；genshin_voice、starrail_voice、wutheringwaves、zenless_voice；1,541,304 条成功，1,882 条失败

### 2026-10-10 · 发布 · tts-ann-transcription-20261010T111659bjt-01

- 时间：2026-10-10 11:17（北京时间）
- manifest：`annotations/transcription/tts-ann-transcription-20261010T111659bjt-01/manifest.json`
- SHA256：`49d5406b7343de2008196d2e9152855f286871e8229bf8c518dda24897fb94af`
- 内容：transcription，qwen-asr-transcription-v2；genshin_voice、starrail_voice、wutheringwaves、zenless_voice；1,543,186 行
- 说明：由 v1 run 迁移为单表，ASR 未重算，逐字段与 v1 一致

### 2026-10-10 · 删除 · tts-ann-transcription-20261009T191644bjt-01

- 原因：已迁移为 `tts-ann-transcription-20261010T111659bjt-01` 单表并逐字段核对
- 替代：`tts-ann-transcription-20261010T111659bjt-01`
- 前提：selection-v1 不使用 annotation，无其他引用

### 2026-10-10 · 发布 · tts-ann-transcription-20261010T171514bjt-import

- 时间：2026-10-10 18:43（北京时间）
- manifest：`annotations/transcription/tts-ann-transcription-20261010T171514bjt-import/manifest.json`
- SHA256：`cd63ba6a11601ebda9b38248633536ab926a9ab031f13f69ee0f77caf448f929`
- 内容：transcription，qwen-asr-transcription-v2；emilia；40,264,231 行

### 2026-10-12 · 发布 · tts-ann-transcription-20261012T092945bjt-01

- 时间：2026-10-12 09:39（北京时间）
- manifest：`annotations/transcription/tts-ann-transcription-20261012T092945bjt-01/manifest.json`
- SHA256：`b29ab6cf67ea7eff4f05ad03b574c532c55962431e437b311ead3b58c72d3f82`
- 内容：transcription，qwen-asr-transcription-v2；emilia；40,264,231 行
- 说明：替代 `tts-ann-transcription-20261010T171514bjt-import`：修正导入器 run_id 后以相同输入重跑，40,264,231 行逐行一致

### 2026-10-12 · 删除 · tts-ann-transcription-20261010T171514bjt-import

- 原因：run_id 不符合 05 的 `bjt-NN` 规则（导入器硬编码 `-import`）
- 替代：`tts-ann-transcription-20261012T092945bjt-01`
- 前提：引用它的 selection `tts-selection-supervised-tts-20261012T045922bjt-01` 已先删除；无其他引用

### 2026-10-13 · 发布 · tts-ann-upstream_quality-20261013T143341bjt-01

- 时间：2026-10-13 16:38（北京时间）
- manifest：`annotations/upstream_quality/tts-ann-upstream_quality-20261013T143341bjt-01/manifest.json`
- SHA256：`be2058a65959dddbab1b8ef68b922ce2e796c014bf9e75983f864058ad45cde0`
- 内容：upstream_quality，upstream-dnsmos-p835-ovrl-v1；emilia 40,264,231 行、emilia_yodas 43,964,905 行、emilia2 99,840,397 行，
  共 184,069,533 行，全部 ok；上游 DNSMOS P.835 校准后 OVRL 原样导入（emilia/yodas `upstream.dnsmos`，emilia2 `upstream.short.dnsmos`）
- 说明：只是导入上游分数，不是本地重算；分布与各数据集上游阈值见 pipeline 仓库 docs/upstream-dnsmos-report.md

### 2026-10-18 · 发布 · tts-ann-transcription-20261010T161238bjt-01

- 时间：2026-10-18 09:26（北京时间）
- manifest：`annotations/transcription/tts-ann-transcription-20261010T161238bjt-01/manifest.json`
- SHA256：`ec10994951af78b6a39fb52254cea2d0eb1bf99ea177c967d39dd08edd825172`
- 内容：transcription，qwen-asr-transcription-v2；emilia_yodas；43,964,905 行，ok 43,964,455，failed 450；
  回读行数与 ID 顺序和固定 base 一致
- 说明：同一 run_id 分两段执行，manifest `segments` 逐段记录 profile。任务 [0, 15423) 用 vLLM 0.18.0
  （qwen3-asr-1.7b-base-v2）；该服务 2026-10-13 停止后，任务 [15423, 85869) 用 sglang-omni
  （qwen3-asr-1.7b-base-v2-sglang）续跑。sglang 段：响应没有 finish_reason，无法判断输出是否到了客户端显式请求的
  max_new_tokens=1024；服务端把超过 30 秒（默认阈值）的音频分块转写，分块后不汇总返回自动检测语言；合并文本里没有
  分块边界标记（英文等加空格、中文等直接连接），边界只能从 raw_response_json 的 segments 恢复。实际影响：超过 30 秒的
  228 行中 sglang 分块 163 行无检测语言；按 selection 规则，无语言行被 2003 排除，到 1024 token 的超长输出远超 1005 字符密度上限

### 2026-10-19 · 发布 · tts-ann-speaker_structure-20261019T093525bjt-01

- 时间：2026-10-19 09:58（北京时间）
- manifest：`annotations/speaker_structure/tts-ann-speaker_structure-20261019T093525bjt-01/manifest.json`
- SHA256：`c6b1ed50a0f527e3bcb2578617ff8ea69d01df1570fcfab937264b5bf82e44e4`
- 内容：speaker_structure，speaker-structure-moss-v1（OpenMOSS MOSS-Transcribe-Diarize，外部 SGLang Omni 服务）；emilia；
  40,264,231 行，ok 40,220,982，failed 43,249（model_invalid_output 38,957、output_truncated 4,292）
- 说明：原计划 emilia、emilia_yodas、emilia2 共用一个 run（`tts-ann-speaker_structure-20261013T175339bjt-01`，未发布），
  2026-10-19 按数据集拆成三个 run，各自完成即发布；本 run 接管原 run 的全部 emilia checkpoint（receipt 摘要、样本顺序、
  输入指纹逐一核对），profile_id 不变，manifest `execution.split_from` 记录来源。第 9 轮只重试了部分失败行即停止
  （失败多为确定性的时间戳越界，重发无改善），发布第 8/9 轮结果

### 2026-10-20 · 发布 · tts-ann-transcription-20261019T143130bjt-01

- 时间：2026-10-20 11:05（北京时间）
- manifest：`annotations/transcription/tts-ann-transcription-20261019T143130bjt-01/manifest.json`
- SHA256：`702fc9212103e3d0387c86cc50a96f43f945b7cad45531ec5c2ec4acfbc8b6d1`
- 内容：transcription，qwen-asr-transcription-v2；hifitts2；12,809,875 行，全部 ok；回读行数与 ID 顺序和固定 base 一致
- 说明：hifitts2 首次转写，全程 sglang-omni（qwen3-asr-1.7b-base-v2-sglang，DP2 GPU 5-6）。该 profile 与 vLLM 版的差别：
  响应没有 finish_reason，无法判断输出是否到了客户端显式请求的 max_new_tokens=1024；超过 30 秒（默认阈值）的音频由服务端
  分块转写，分块后不汇总返回自动检测语言，合并文本里没有分块边界标记（边界可从 raw_response_json 的 segments 恢复）。
  hifitts2 最长 20 秒，后两项不涉及。抽样 3 万行对上游文本：
  WER 3.06%、CER 1.36%，大部分差异来自上游 OCR/对齐/朗读偏差

### 2026-10-21 · 发布 · tts-ann-effective_bandwidth-20261020T153104bjt-01

- 时间：2026-10-21 10:36（北京时间）
- manifest：`annotations/effective_bandwidth/tts-ann-effective_bandwidth-20261020T153104bjt-01/manifest.json`
- SHA256：`12e5abdbf3bf79557ee3fea21c686793b895bcd23e47c37f2629aec964de6ce2`
- 内容：effective_bandwidth（welch-band-edge-v1）；emilia；40,264,231 行；输入 emilia/v0.1；schema effective-bandwidth-v1
- 覆盖：`{"failed": 0, "missing": 0, "ok": 40264231, "skipped": 0, "total_targets": 40264231, "unsupported": 0}`
- 核验：checkpoint 校验、完整内容回读、固定版本行数核对；两份计算全部完成后发布。


### 2026-10-21 · 发布 · tts-ann-effective_bandwidth-20261020T155412bjt-01

- 时间：2026-10-21 10:50（北京时间）
- manifest：`annotations/effective_bandwidth/tts-ann-effective_bandwidth-20261020T155412bjt-01/manifest.json`
- SHA256：`702996529c4eaf0a06d310665db50d699bd910b2d16007464e1cfb8dbaea6e02`
- 内容：effective_bandwidth（welch-band-edge-v1）；emilia_yodas；43,964,905 行；输入 emilia_yodas/v0.1；schema effective-bandwidth-v1
- 覆盖：`{"failed": 0, "missing": 0, "ok": 43964905, "skipped": 0, "total_targets": 43964905, "unsupported": 0}`
- 核验：checkpoint 校验、完整内容回读、固定版本行数核对；两份计算全部完成后发布。


### 2026-10-22 · 发布 · tts-ann-speaker_structure-20261021T124643bjt-01

- 时间：2026-10-22 09:23（北京时间）
- manifest：`annotations/speaker_structure/tts-ann-speaker_structure-20261021T124643bjt-01/manifest.json`
- SHA256：`75bd3456822b060fd10fe02d98e42c11b320c624df07cac2b58ad0291b269b21`
- 内容：speaker_structure（moss-transcribe-diarize-v1）；genshin_voice、starrail_voice、wutheringwaves、zenless_voice；1,543,186 行（ok 1,537,846，failed 5,340）；输入为四个数据集 v0.1；schema speaker-structure-moss-v1
- genshin_voice：覆盖 `{"failed": 1103, "missing": 0, "ok": 653149, "skipped": 0, "total_targets": 654252, "unsupported": 0}`；错误码 `{"model_invalid_output": 773, "output_truncated": 330}`
- starrail_voice：覆盖 `{"failed": 1195, "missing": 0, "ok": 402242, "skipped": 0, "total_targets": 403437, "unsupported": 0}`；错误码 `{"model_invalid_output": 380, "output_truncated": 814, "request_timeout": 1}`
- wutheringwaves：覆盖 `{"failed": 804, "missing": 0, "ok": 77973, "skipped": 0, "total_targets": 78777, "unsupported": 0}`；错误码 `{"model_invalid_output": 75, "output_truncated": 729}`
- zenless_voice：覆盖 `{"failed": 2238, "missing": 0, "ok": 404482, "skipped": 0, "total_targets": 406720, "unsupported": 0}`；错误码 `{"model_invalid_output": 1600, "output_truncated": 563, "request_timeout": 75}`
- 核验：checkpoint 校验、完整字段回读、样本 ID 顺序与固定 base 一致、固定 Lance 版本行数核对，missing 为 0。
- 说明：用户授权直接发布首轮结果；与此前 Emilia MOSS 发布一致，保留失败样本的状态和错误码，不补跑失败样本。


### 2026-10-23 · 发布 · tts-ann-speaker_structure-20261019T093525bjt-02

- 时间：2026-10-23 12:11（北京时间）
- manifest：`annotations/speaker_structure/tts-ann-speaker_structure-20261019T093525bjt-02/manifest.json`
- SHA256：`e19431d9c3f0668252c42bd1501a50ebd6854317c372442646b2dd8aaa35d9bf`
- 内容：speaker_structure（moss-transcribe-diarize-v1）；emilia_yodas；43,964,905 行（ok 43,930,224，failed 34,681）；输入为 emilia_yodas/v0.1；schema speaker-structure-moss-v1
- emilia_yodas：覆盖 `{"failed": 34681, "missing": 0, "ok": 43930224, "skipped": 0, "total_targets": 43964905, "unsupported": 0}`；错误码 `{"model_invalid_output": 30523, "output_truncated": 4158}`
- 核验：checkpoint 校验、完整字段回读、样本 ID 顺序与固定 base 一致、固定 Lance 版本行数核对，missing 为 0。
- 说明：用户授权发布已完成的第 17 轮结果；与此前 Emilia MOSS 发布一致，保留失败样本的状态和错误码，不补跑失败样本。

### 2026-11-03 · 发布 · tts-ann-nv_tag_text-20261103T171702bjt-01

- 时间：2026-11-03 17:17（北京时间）
- manifest：`annotations/nv_tag_text/tts-ann-nv_tag_text-20261103T171702bjt-01/manifest.json`
- SHA256：`a5c100f9dd6876cbd95241217968d6b73b7469c95c13c5415dd12f7cc60f22e9`
- 内容：text revision `{action, text}`，映射 nv-canonical-v1（18 个 NVSpeech 标签 + 8 个扩展标签）；emilia_nv 174,179 行全 keep；
  smiip_nv 17,377（ok 17,087，skipped 290：仅区间式标注 287、无文本 3）；nonverbalspeech38k 38,718 全 ok；nonverbaltts 6,256（ok 6,251，无文本 5）；
  disfluencyspeech 5,000 全 ok。每行记 source_tags、target_tags、extended、fixes
- 说明：映射表、计数、上游笔误修复与推断项（NonverbalTTS emoji 含义按频次顺序推断）见 [NV 标签对齐](../nv-tags.md)

## Selection

### 2026-09-29 · 发布 · tts-selection-supervised-tts-20260929T151539bjt-01

- 时间：2026-09-29 15:39（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20260929T151539bjt-01/manifest.json`
- SHA256：`8973f6876a34b0d618acf8326773a1f79b8d7a6dfdd0d280996c988c1172ef9b`
- 内容：规则 selection-v1；16 个数据集；入选 128,220,178 / 134,832,658 条，357,506.8 小时
- 说明：rules.json 无 rules_version 字段（发布早于命名约定）；训练仓库 LM-TTS-Training/data/builds 的两个 build 使用

### 2026-10-12 · 发布 · tts-selection-supervised-tts-20261012T045922bjt-01

- 时间：2026-10-12 05:44（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261012T045922bjt-01/manifest.json`
- SHA256：`25c37e2191df321ee1b480c20af2ec1e9d34862bb4c89121ff729ae62082bc08`
- 内容：规则 selection-filter-v1；18 个数据集；入选 204,053,470 / 235,079,775 条，477,713.3 小时；父 selection `tts-selection-supervised-tts-20260929T151539bjt-01`
- 说明：rules.json 中 rules_version 为旧名 selection-filter-v1

### 2026-10-12 · 发布 · tts-selection-supervised-tts-20261012T095348bjt-01

- 时间：2026-10-12 10:52（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261012T095348bjt-01/manifest.json`
- SHA256：`2044026bb0537d411b132e46a1d0673f591a0bff25c83751bc0bc92a46ae1bb4`
- 内容：规则 selection-v2；18 个数据集；入选 204,053,470 / 235,079,775 条，477,713.3 小时；父 selection `tts-selection-supervised-tts-20260929T151539bjt-01`
- 说明：替代 `tts-selection-supervised-tts-20261012T045922bjt-01`：改正绑定的 Emilia 导入 run 与 rules_version 后按相同规则重新发布，各数据集原因计数与旧发布一致；当前一阶段预训练 selection

### 2026-10-12 · 删除 · tts-selection-supervised-tts-20261012T045922bjt-01

- 原因：绑定 run_id 不合规的 `tts-ann-transcription-20261010T171514bjt-import`，rules_version 为旧名
- 替代：`tts-selection-supervised-tts-20261012T095348bjt-01`
- 前提：没有特征或训练 build 引用；18 个分支与 selection 目录一并删除

### 2026-10-12 · 发布 · tts-selection-supervised-tts-20261012T123927bjt-01

- 时间：2026-10-12 13:31（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261012T123927bjt-01/manifest.json`
- SHA256：`f30e11b782b8da48b4848bbcb65d7567dda3452f0a0bdd020c64bc927fc803c4`
- 内容：规则 selection-v2；18 个数据集；入选 203,998,904 / 235,079,775 条，477,595.7 小时；父 selection `tts-selection-supervised-tts-20260929T151539bjt-01`
- 说明：selection-v2 规则增加上游测试集留出（metadata original_split 为 test 类，7001 held_out；dev 保留；精确重复副本一并留出）后重新发布，
  替代 `tts-selection-supervised-tts-20261012T095348bjt-01`。与之相比仅 5 个数据集的测试集行由 0 改为 7001：aishell3 24,751、mls_sidon 17,696、libritts_r 9,295、hifitts2 1,943、hifitts 881，
  其余原因计数与重复分组不变；当前一阶段预训练 selection

### 2026-10-12 · 删除 · tts-selection-supervised-tts-20261012T095348bjt-01

- 原因：selection-v2 规则增加上游测试集留出后重新发布；selection-v2 只保留一份发布
- 替代：`tts-selection-supervised-tts-20261012T123927bjt-01`
- 前提：没有特征或训练 build 引用；18 个分支、保护 tag 与 selection 目录一并删除

### 2026-10-16 · 发布 · tts-selection-supervised-tts-20261016T115213bjt-01

- 时间：2026-10-16 12:55（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261016T115213bjt-01/manifest.json`
- SHA256：`ed82970f6e9cb05d88d91b3b87678a202bbcd32f57e619167e35e492475a872b`
- 内容：规则 selection-v3（首版，无 parent_selected_only）；emilia2；入选 7,848,897 / 99,840,397 条，22,599.3 小时；父 selection `tts-selection-supervised-tts-20261012T123927bjt-01`；
  质量输入 `tts-ann-upstream_quality-20261013T143341bjt-01`（upstream DNSMOS ≥ 3.3655）
- 说明：只在 emilia2 内部去重，24 行在 selection-v2 中是 emilia/emilia_yodas 副本的非代表（4001 22、4002 2）却重新入选，不是 selection-v2 的子集

### 2026-10-16 · 发布 · tts-selection-supervised-tts-20261016T131121bjt-01

- 时间：2026-10-16 13:59（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261016T131121bjt-01/manifest.json`
- SHA256：`e0558ae8516bd10c507f2dbf4c5ed1b1bc7f84228a9070021cbbcf5aeadc2cca`
- 内容：规则 selection-v3（parent_selected_only，字典 version 4）；emilia2；入选 7,848,873 / 99,840,397 条，22,599.3 小时；父 selection `tts-selection-supervised-tts-20261012T123927bjt-01`；
  质量输入 `tts-ann-upstream_quality-20261013T143341bjt-01`（upstream DNSMOS ≥ 3.3655）；8001 共 34,208 行（替换父原因 4001 30,880、4002 3,328）
- 说明：替代 `tts-selection-supervised-tts-20261016T115213bjt-01`；逐行核对入选行全部在 selection-v2 中入选，与旧发布只差上述 24 行。
  特征按行筛选 selection-v2 已发布的 emilia2 codec/text/speaker/merged，不另行提取

### 2026-10-16 · 删除 · tts-selection-supervised-tts-20261016T115213bjt-01

- 原因：不是 selection-v2 的子集（见其发布小节）
- 替代：`tts-selection-supervised-tts-20261016T131121bjt-01`
- 前提：没有特征或训练 build 引用；emilia2 上的分支、保护 tag 与 selection 目录一并删除

### 2026-10-16 · 删除 · tts-selection-supervised-tts-20261016T131121bjt-01

- 原因：全局单一 DNSMOS 门槛偏向长片段与 en/zh（短片段补齐打分偏低、各语言上游门槛不同）；改按语言 × 时长段分组取高分重新定义 selection-v3
- 替代：无（新规则另行发布）
- 前提：没有特征或训练 build 引用（特征任务只建到规划即停止，未发布）；emilia2 上的分支、保护 tag 与 selection 目录一并删除

### 2026-10-19 · 发布 · tts-selection-supervised-tts-20261019T103416bjt-01

- 时间：2026-10-19 11:02（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261019T103416bjt-01/manifest.json`
- SHA256：`39c9b6269891c9f00ca799de10c962122618034ad9442df36fb924677ad3177a`
- 内容：selection-v4（原因/flag 字典 version 5）；只有 emilia，40,264,231 行，入选 33,992,131 条，84,158.5 小时
  （en 40,359 h、zh 39,493 h、ja 1,476 h、de 1,420 h、fr 1,214 h、ko 198 h）。规则照抄 selection-v2，文本仍为
  `tts-ann-transcription-20261012T092945bjt-01`；新增说话人输入 `tts-ann-speaker_structure-20261019T093525bjt-01`，
  不是恰好 1 人记 6002（2 人 3,481,881、3 人 157,311、4+ 人 13,285、0 人 12,753、诊断失败 43,249）。
  父 selection 为 `tts-selection-supervised-tts-20261012T123927bjt-01`，继承其 14,258 条有效排除，无新增
- 说明：与 selection-v2 的 emilia 逐行比对：33,991,838 条两者都入选；v2 入选而 v4 未选的 3,437,142 条全部是 6002；
  v4 入选而 v2 未选 293 条（v2 原因 4001 236 条、4002 57 条，同音频组原代表被 6002 排除后由组内成员接替）。
  这 293 条没有 selection-v2 的特征，训练构建按用户决定跳过，清单见 pipeline 仓库工作区
  `reports/selection-v4/selected-without-v2-features.tsv`（与 reports/interval-audit 一样不入 git）（SHA256 `d113358f3de1d3dda29dc3bd63144cbd24f1ec740dfcee5c7b9d395e6678c8e4`）；
  其余行可直接按行筛选 selection-v2 的 emilia 特征。emilia_yodas 的 diarization 完成后以新的 selection_id 发布含 emilia_yodas 的 v4

### 2026-11-03 · 发布 · tts-selection-supervised-tts-20261103T173114bjt-01

- 时间：2026-11-03 17:31（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261103T173114bjt-01/manifest.json`
- SHA256：`cef4d2efc79ec5fb410a269788d4bee9d7790237fe815b337d91506eb3d1edbe`
- 内容：selection-nv-v1，字典 version 7；入选 230,083 条 / 730.8 小时：emilia_nv 173,487、nonverbalspeech38k 35,589、smiip_nv 14,513、
  disfluencyspeech 4,750、nonverbaltts 1,744（仅 Expresso 48 kHz）。文本来自 `tts-ann-nv_tag_text-20261103T171702bjt-01`；
  主要排除：nonverbaltts 16 kHz 4,452（3003）、smiip_nv spk_test 2,574 与 disfluencyspeech test 250（7001）、
  nonverbalspeech38k 上游重复 3,129（4001/4002）、smiip_nv 无可用标注 290（1003）
- 说明：规则见 [12 · selection-nv-v1](specs/12-selections.md#selection-nv-v1)。同日 17:21 曾发布 `tts-selection-supervised-tts-20261103T172155bjt-01`，
  其实现摘要对应未提交的中间代码，未登记、未被引用，已删除分支、tag 与目录后按提交代码重新发布（结果相同）

### 2026-11-03 · 发布 · tts-selection-supervised-tts-20261103T190044bjt-01

- 时间：2026-11-03 19:01（北京时间）
- manifest：`selections/tts-selection-supervised-tts-20261103T190044bjt-01/manifest.json`
- SHA256：`a1a88c83d00b94ce4e3b5bae2339d788e90c7d572dc59444e4a638780f2d01f8`
- 内容：selection-game-zhen-v1（selection-v2 规则只用于 genshin_voice、starrail_voice、zenless_voice、wutheringwaves，语言限 en/zh；
  文本为 v2 同一转写 annotation `tts-ann-transcription-20261010T111659bjt-01`；父 selection 为 selection-v2，继承其排除）；
  入选 680,109 条 / 1,090.6 小时：genshin 293,546、starrail 184,843、zenless 165,223、wutheringwaves 36,497
- 说明：重复只在这四个数据集内按 audio_sha256 裁决。与 emilia_nv 的重编码重复（genshin/starrail 约 1.15 万条，音频哈希不同）
  不在 selection 处理，由训练端 build 去重；配对证据 `reports/nv-game-overlap/loose_pairs.jsonl`（不入统一根）

## Features

### 2026-09-30 · 发布 · text · text-selection-text-v1-20260930T153654bjt-01

- 时间：最后一个数据集完成于 2026-09-30 17:01（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/text/<dataset_id>-text-selection-text-v1-20260930T153654bjt-01/manifest.json`
- 内容：16 个数据集，128,220,178 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`

### 2026-10-01 · 发布 · speaker_embedding · speaker_embedding-qwen3-ecapa-24k-fp32-packed-v1-20260930T130829bjt-01

- 时间：最后一个数据集完成于 2026-10-01 05:34（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/speaker_embedding/<dataset_id>-speaker_embedding-qwen3-ecapa-24k-fp32-packed-v1-20260930T130829bjt-01/manifest.json`
- 内容：16 个数据集，128,220,178 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`

### 2026-10-01 · 发布 · codec · codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20260930T181155bjt-01

- 时间：最后一个数据集完成于 2026-10-01 13:33（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/codec/<dataset_id>-codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20260930T181155bjt-01/manifest.json`
- 内容：12 个数据集，49,918,118 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`

### 2026-10-01 · 发布 · codec · codec-qwen3-12hz-24k-k16-fp32-packed-v1-20260930T024146bjt-01

- 时间：最后一个数据集完成于 2026-10-01 14:56（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/codec/<dataset_id>-codec-qwen3-12hz-24k-k16-fp32-packed-v1-20260930T024146bjt-01/manifest.json`
- 内容：4 个数据集，78,302,060 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`
- 说明：其余 12 个数据集由 packed-actual-v2 重跑，本 run 在这些数据集上未发布

### 2026-10-01 · 发布 · merged · merged-selected-features-v1-20261001T161244bjt-01

- 时间：最后一个数据集完成于 2026-10-01 18:09（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/merged/<dataset_id>-merged-selected-features-v1-20261001T161244bjt-01/manifest.json`
- 内容：16 个数据集，128,220,178 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`

### 2026-10-09 · 发布 · speaker_embedding · speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261008T131631bjt-01

- 时间：最后一个数据集完成于 2026-10-09 00:01（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/speaker_embedding/<dataset_id>-speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261008T131631bjt-01/manifest.json`
- 内容：16 个数据集，128,220,178 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`

### 2026-10-09 · 发布 · merged · merged-reference-features-v1-20261009T000157bjt-01

- 时间：最后一个数据集完成于 2026-10-09 02:32（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/merged/<dataset_id>-merged-reference-features-v1-20261009T000157bjt-01/manifest.json`
- 内容：16 个数据集，128,094,617 行；绑定 selection `tts-selection-supervised-tts-20260929T151539bjt-01`

### 2026-10-12 · 发布 · text · text-selection-text-v1-20261012T140653bjt-01

- 时间：最后一个数据集完成于 2026-10-12 18:31（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/text/<dataset_id>-text-selection-text-v1-20261012T140653bjt-01/manifest.json`
- 内容：18 个数据集，203,998,904 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`

### 2026-10-13 · 发布 · speaker_embedding · speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261012T193900bjt-01

- 时间：最后一个数据集完成于 2026-10-13 00:39（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/speaker_embedding/<dataset_id>-speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261012T193900bjt-01/manifest.json`
- 内容：17 个数据集，125,601,114 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`
- 说明：early 批次（除 emilia2 外的 17 个数据集），与 emilia2 的 codec 编码并行；失败 1 类 no_legal_reference

### 2026-10-13 · 发布 · merged · merged-reference-features-v1-20261013T004007bjt-01

- 时间：最后一个数据集完成于 2026-10-13 01:24（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/merged/<dataset_id>-merged-reference-features-v1-20261013T004007bjt-01/manifest.json`
- 内容：17 个数据集，125,485,003 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`
- 说明：排除 codec/speaker 非 ok 行 116,111 行

### 2026-10-13 · 发布 · codec · codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261012T134948bjt-01

- 时间：最后一个数据集完成于 2026-10-13 02:50（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/codec/<dataset_id>-codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261012T134948bjt-01/manifest.json`
- 内容：18 个数据集，203,998,904 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`
- 说明：125.2M 行由已发布 codec 结果按 audio_sha256 复用；emilia、mls_sidon 的发布在 ACP 节点上因共享文件系统丢失 rename 失败重试多次后成功，失败尝试留下的旧版本已于 2026-10-13 清理（见统一根维护）

### 2026-10-13 · 发布 · speaker_embedding · speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261013T025148bjt-01

- 时间：最后一个数据集完成于 2026-10-13 06:35（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/speaker_embedding/<dataset_id>-speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261013T025148bjt-01/manifest.json`
- 内容：1 个数据集（emilia2），78,397,790 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`
- 说明：late 批次（emilia2），全部新提取；no_legal_reference 1,713,382 行（2.19%）

### 2026-10-13 · 发布 · merged · merged-reference-features-v1-20261013T063612bjt-01

- 时间：最后一个数据集完成于 2026-10-13 07:00（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/merged/<dataset_id>-merged-reference-features-v1-20261013T063612bjt-01/manifest.json`
- 内容：1 个数据集（emilia2），76,684,408 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`
- 说明：排除 speaker no_legal_reference 1,713,382 行；最终全量审计在核对本数据集时按用户要求暂停，核对完成前视为待确认

### 2026-10-13 · 修正 · merged · merged-reference-features-v1-20261013T063612bjt-01 登记说明

- 时间：2026-10-13 12:51（北京时间）
- 原因：发布小节登记时最终全量审计尚未完成，emilia2 的 merged 核对标为待确认
- 处理：最终全量审计完成：selection-v2 的 72 个 final（codec、text、speaker_embedding、merged 各 18 个）0 个问题，全量读取 814.2M 行、3.84 TB；merged emilia2 的 target_id 流与 text 减去 speaker no_legal_reference 行完全一致，targets.lance 与子集摘要一致。审计时的 2 条警告（codec emilia、mls_sidon 仅被旧版本引用的文件）已由 2026-10-13 的清理消除，复核为 0 警告
- 原/新 SHA256：manifest 未改动
- 证据：selection-v2 feature run 工作目录 `audit/audit-final-20261012T1603.json`；审计工具 `scripts/audit_features.py`

### 2026-10-16 · 发布 · speaker_embedding · emilia2-speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261016T103356bjt-01

- 时间：2026-10-16 14:20（北京时间）
- manifest：`datasets/emilia2/v0.1/features/speaker_embedding/emilia2-speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261016T103356bjt-01/manifest.json`
- SHA256：`8924aeae598e4afee1d1db30eac0ba3af005a8fb21e8afeca229a048fb1037f1`
- 内容：emilia2，78,397,790 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`；profile `qwen3-ecapa-24k-fp32-prefix-reference-v1`（参考从第 0 帧开始，长度与 uniform 策略相同，seed 20261008）
- 说明：ACP 任务 pt-avtoatx9，codec/text 固定为 selection-v2-20261012 已发布结果；全部新提取；no_legal_reference 1,713,382 行，与 uniform 版一致

### 2026-10-16 · 发布 · merged · emilia2-merged-prefix-reference-features-v1-20261016T142123bjt-01

- 时间：2026-10-16 14:43（北京时间）
- manifest：`datasets/emilia2/v0.1/features/merged/emilia2-merged-prefix-reference-features-v1-20261016T142123bjt-01/manifest.json`
- SHA256：`7ff9ae9259402839faf630ca7fd9f71b90eb43bdb42c448763e68c4bc8f8f6d5`
- 内容：emilia2，76,684,408 行；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`；profile `prefix-reference-features-v1`
- 说明：排除 speaker no_legal_reference 1,713,382 行，行数与 uniform 版 `merged-reference-features-v1-20261013T063612bjt-01` 一致；供另一 pretrain 使用，uniform 版保留

### 2026-10-19 · 发布 · codec · emilia-codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261019T120241bjt-01

- 时间：2026-10-19 13:10（北京时间）
- manifest：`datasets/emilia/v0.1/features/codec/emilia-codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261019T120241bjt-01/manifest.json`
- SHA256：`34e9385ef14964e1d3be4d3a2d10bf0903d0ca0071ce8359d4ca1689e17809bc`
- 内容：emilia，33,992,131 行；绑定 selection `tts-selection-supervised-tts-20261019T103416bjt-01`（selection-v4）
- 说明：复用 `codec-qwen3-12hz-24k-k16-fp32-packed-actual-v2-20261012T134948bjt-01`（selection-v2）的 33,991,838 行（同 audio_sha256 与采样率，payload 复核、身份重绑），其余 293 行在本机 GPU 6 编码

### 2026-10-19 · 发布 · text · emilia-text-selection-text-v1-20261019T120740bjt-01

- 时间：2026-10-19 13:51（北京时间）
- manifest：`datasets/emilia/v0.1/features/text/emilia-text-selection-text-v1-20261019T120740bjt-01/manifest.json`
- SHA256：`2b9311a029bff1355cdc5729d9e2f1e5605e9b77f842bbbd260392d702d84e32`
- 内容：emilia，33,992,131 行；绑定 selection-v4
- 说明：文本来源同 selection-v2（`tts-ann-transcription-20261012T092945bjt-01`）

### 2026-10-19 · 发布 · speaker_embedding · emilia-speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261019T131032bjt-01

- 时间：2026-10-19 14:35（北京时间）
- manifest：`datasets/emilia/v0.1/features/speaker_embedding/emilia-speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261019T131032bjt-01/manifest.json`
- SHA256：`d5d8ccfda897fe8db77f4f4e7a0dcb83d81c24e770356052bd42819ed84c6774`
- 内容：emilia，33,992,131 行；绑定 selection-v4；uniform reference（seed 20261008）
- 说明：复用 `speaker_embedding-qwen3-ecapa-24k-fp32-reference-v1-20261012T193900bjt-01` 的 33,991,838 行（同 profile、音频与参考坐标），其余 293 行在本机 GPU 6 计算

### 2026-10-19 · 发布 · merged · emilia-merged-reference-features-v1-20261019T143526bjt-01

- 时间：2026-10-19 14:51（北京时间）
- manifest：`datasets/emilia/v0.1/features/merged/emilia-merged-reference-features-v1-20261019T143526bjt-01/manifest.json`
- SHA256：`e02bf7159be6de7d265f40c7bf0902fb27b1704c01322e69ff447384877a4ecd`
- 内容：emilia，33,992,131 行；绑定 selection-v4；profile `reference-features-v1`
- 说明：四份在本机（非 ACP）由 `feature_pipeline.py` 生成，work `artifacts/feature-runs/selection-v4-20261019`；发布后审计 CLEAN（136.0M 行 / 653.2 GB 全量读取，0 问题）

### 2026-10-19 · 发布 · speaker_embedding · emilia-speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261019T150157bjt-01

- 时间：2026-10-19 17:25（北京时间）
- manifest：`datasets/emilia/v0.1/features/speaker_embedding/emilia-speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261019T150157bjt-01/manifest.json`
- SHA256：`a35ece3156b005f5a5db2c013e8fdf3291e7642b4cde5b8fa722d1bb1630936c`
- 内容：emilia，37,428,980 行，全部 ok；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`（selection-v2）；prefix reference（每条参考从第 0 帧开始，长度同 uniform 策略，seed 20261008）
- 说明：ACP 任务 pt-pzl532uk（work `artifacts/feature-runs/selection-v2-emilia-yodas-prefix-20261019`），固定 selection-v2 的 codec/text 发布，emilia 与 emilia_yodas 同一任务计算；作为 selection-v4 及之后含 emilia_yodas 的 v4 的 prefix speaker 复用来源；发布后审计 CLEAN（40 份 final、559.7M 行 / 2079.8 GB）

### 2026-10-19 · 发布 · speaker_embedding · emilia_yodas-speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261019T150157bjt-01

- 时间：2026-10-19 17:41（北京时间）
- manifest：`datasets/emilia_yodas/v0.1/features/speaker_embedding/emilia_yodas-speaker_embedding-qwen3-ecapa-24k-fp32-prefix-reference-v1-20261019T150157bjt-01/manifest.json`
- SHA256：`871b014a79b7d2a9ed75ebf76dc8a1f06fe887ab7b3bbca9fe11437f044592f6`
- 内容：emilia_yodas，38,415,488 行，全部 ok；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`（selection-v2）；prefix reference（每条参考从第 0 帧开始，长度同 uniform 策略，seed 20261008）
- 说明：ACP 任务 pt-pzl532uk（work `artifacts/feature-runs/selection-v2-emilia-yodas-prefix-20261019`），固定 selection-v2 的 codec/text 发布，emilia 与 emilia_yodas 同一任务计算；作为 selection-v4 及之后含 emilia_yodas 的 v4 的 prefix speaker 复用来源；发布后审计 CLEAN（40 份 final、559.7M 行 / 2079.8 GB）

### 2026-10-19 · 发布 · merged · emilia-merged-prefix-reference-features-v1-20261019T174217bjt-01

- 时间：2026-10-19 17:55（北京时间）
- manifest：`datasets/emilia/v0.1/features/merged/emilia-merged-prefix-reference-features-v1-20261019T174217bjt-01/manifest.json`
- SHA256：`65da354d1659bce2c2421ffc7c96310c30bd6817fe4c3b0d4729cea6a819e2a9`
- 内容：emilia，37,428,980 行，无排除；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`（selection-v2）；profile `prefix-reference-features-v1`
- 说明：ACP 任务 pt-pzl532uk（work `artifacts/feature-runs/selection-v2-emilia-yodas-prefix-20261019`），固定 selection-v2 的 codec/text 发布，emilia 与 emilia_yodas 同一任务计算；作为 selection-v4 及之后含 emilia_yodas 的 v4 的 prefix speaker 复用来源；发布后审计 CLEAN（40 份 final、559.7M 行 / 2079.8 GB）

### 2026-10-19 · 发布 · merged · emilia_yodas-merged-prefix-reference-features-v1-20261019T174217bjt-01

- 时间：2026-10-19 18:01（北京时间）
- manifest：`datasets/emilia_yodas/v0.1/features/merged/emilia_yodas-merged-prefix-reference-features-v1-20261019T174217bjt-01/manifest.json`
- SHA256：`ddccdf600e0cb1aa799afb9ec7b88c4ae4d99a504811d6f05326f9ea0954a11a`
- 内容：emilia_yodas，38,415,488 行，无排除；绑定 selection `tts-selection-supervised-tts-20261012T123927bjt-01`（selection-v2）；profile `prefix-reference-features-v1`
- 说明：ACP 任务 pt-pzl532uk（work `artifacts/feature-runs/selection-v2-emilia-yodas-prefix-20261019`），固定 selection-v2 的 codec/text 发布，emilia 与 emilia_yodas 同一任务计算；作为 selection-v4 及之后含 emilia_yodas 的 v4 的 prefix speaker 复用来源；发布后审计 CLEAN（40 份 final、559.7M 行 / 2079.8 GB）

### 2026-11-02 · 发布 · dots_vae · emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v1-20261102T173741bjt-01

- 时间：2026-11-02 20:54（北京时间）
- manifest：`datasets/emilia_nv/v0.1/features/dots_vae/emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v1-20261102T173741bjt-01/manifest.json`
- SHA256：`58abf8c8e6ee0673adefb11b19029b354a63fb1ca53a037bf99ff93f7e73b5d0`
- 内容：emilia_nv，174,179 行（all_samples，不绑定 selection），全部 ok；profile `dots-tts-soar-vae-xvec-48k-fp32-v1`（`045359a75b8b08fae37a0fb2aa453e2a60714e655b47e1414df7654157d0326b`）；
  每行 25 Hz AudioVAE posterior（vae_mean/vae_logstd 各 128 维，共 53,552,708 帧）+ CAM++ 512 维 x-vector，带原文本（含副语言标签）
- 说明：按 dots.tts 训练波形链（48k kaiser 重采样、边缘静音 250 ms、7680 对齐）调用上游 4cf7d6b 函数生成，fp32；x-vector 取前 10 s（训练为随机 10 s）；
  latent_stats 未施加，由训练端归一化；表 52 GB，target_id/feature_key BTREE 索引

### 2026-11-03 · 发布 · dots_vae · emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T095118bjt-01

- 时间：2026-11-03 11:25（北京时间）
- manifest：`datasets/emilia_nv/v0.1/features/dots_vae/emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T095118bjt-01/manifest.json`
- SHA256：`4d8f40ad400b90bee1c1740019c7a7fcc6451b5a6a4e986de20dbb1cacbcecad`
- 内容：emilia_nv，174,179 行（all_samples，不绑定 selection），全部 ok；profile `dots-tts-soar-vae-xvec-48k-fp32-v2`；
  v1 的全部列（逐行与 v1 的 latents_sha256、xvector_sha256 一致）加训练端参考段：reference_start_patch/reference_end_patch（半开，1 patch = 7680 采样 = 0.16 s）
  与该段的 CAM++ x-vector（reference_xvector，不截 10 s，不含对齐补零）；规则 `dots-reference-v1:codec-grid-v1-on-patches`
  （codec-grid-reference-v1 移到 patch 网格：长度在 [max(4, ceil(0.1 N)), N // 2] 均匀，再起点均匀，取自 sha256(target_id)）；67 行过短无参考段（三列为空）
- 说明：替代 v1；参考段 x-vector 防止整句 x-vector 泄露目标句韵律与副语言事件。表 52 GB

### 2026-11-03 · 删除 · dots_vae · emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v1-20261102T173741bjt-01

- 原因：x-vector 取整句前 10 s，泄露目标内容；用户要求删除
- 替代：`emilia_nv-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T095118bjt-01`
- 前提：训练端已删除引用它的 build（data/builds/dots-emilia-nv-20261102）并确认改用 v2；统一根内无其他引用；v2 发布并逐行核对后删除

### 2026-11-03 · 发布 · dots_vae · <dataset_id>-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T173207bjt-01

- 时间：2026-11-03 18:34（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/dots_vae/<dataset_id>-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T173207bjt-01/manifest.json`
- SHA256：disfluencyspeech `08c55817697ab8f787923484ce57b3eef6eb8a163d5cb65f08ed1f1196131648`；
  emilia_nv `8a046c970de675e8595c0f2ea6101a27400d6bc0d6f49c36b738b8359c664527`；
  nonverbalspeech38k `2c322f351d1cc727d18c4ebf1575206bc9e090fa4a89da82e7184d0031fbf02e`；
  nonverbaltts `7bb6bf9d69a1bfe2b23910034e3295646d6ac5e922c6a55584b8427657a2d13e`；
  smiip_nv `afdc8b3cb2b903a68eed4ebe49edd2b7f2d3274886572c05ccbc66708e6a08e0`
- 内容：5 个数据集 230,083 行全部 ok；selection_branch 绑定 `tts-selection-supervised-tts-20261103T173114bjt-01`；
  text 为选用文本（逐行与 selection 一致），text_kind=`nv-canonical-v1`，manifest.text 记 annotation run 与映射版本；
  核对全部文本只含 26 个标签、无残留标记；profile 同 `dots-tts-soar-vae-xvec-48k-fp32-v2`
- 说明：emilia_nv 的 x-vector 与 `emilia_nv-…-v2-20261103T095118bjt-01` 逐行一致；13,506 行 latent 有 fp32 舍入级差异（最大 1.3e-5），
  因 selection 去掉部分行后 GPU batch 组成不同，与批量/单条差异同量级

### 2026-11-03 · 发布 · dots_vae · <dataset_id>-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T190254bjt-01

- 时间：2026-11-03 22:46（北京时间）
- manifest：`datasets/<dataset_id>/v0.1/features/dots_vae/<dataset_id>-dots_vae-dots-tts-soar-vae-xvec-48k-fp32-v2-20261103T190254bjt-01/manifest.json`
- SHA256：genshin_voice `ceee5f13dbdd61a35dffbf820e8dddea9445cde0d69ea31476e8e17a30c14222`；
  starrail_voice `f9f31a025f6fb51be09d9aecbe77df8338a7d022a554c1262ffbfda0315109b0`；
  wutheringwaves `0cea665b8d8139776df4a86bab07c332a56703304cb06a0ff66c11aee16b75fb`；
  zenless_voice `f7e6568aff6d6b607baa8fad8b029cbe30f68f8cb9f05eff88119df0a3f97ee0`
- 内容：680,109 行全部 ok；selection_branch 绑定 `tts-selection-supervised-tts-20261103T190044bjt-01`；text 为选用的 ASR 转写（逐行与 selection 一致），
  text_kind=`asr_transcription`；language 列保留基础原始标签（en-US、zh-CN 等）

## 统一根维护

### 2026-09-29 · 清理 · 已发布 release 的孤儿临时文件

- 内容：9 个数据集 samples.lance/data 下 268 个未被任何保留版本引用的 `.tmp*`（150.705 GB），以及 MLS `.state/v0.1/identity.sqlite`（3.11 GB）
- 核验：清理后 16 个数据集的 manifest SHA256、行数、版本列表与数据文件引用均不变
- 证据：pipeline 仓库 docs/design-review/published-cleanup-review.md

### 2026-10-12 · 清理 · 非发布残留

- 内容：17 个数据集的 `.state/` 与 `v0.1.lock`（已完成转换的恢复状态，约 25 GB）；12 个未发布的 codec packed-v1 `.incomplete` 目录；未发布 audio_stats run 留下的空 `annotations/audio_stats/`
- 核验：没有 manifest 引用被删路径；统一根下不再有 `.incomplete`、`.state`、`.lock`

### 2026-10-13 · 清理 · codec emilia、mls_sidon 失败发布遗留的旧版本

- 内容：`emilia`、`mls_sidon` 的 codec `…-packed-actual-v2-20261012T134948bjt-01` 在 ACP 节点上发布失败时提交过的旧 Lance 版本（emilia v1–v11，31,145 个文件，15.71 GB；mls_sidon v1–v7，7,700 个文件，2.15 GB），只保留 manifest 与 run_id 标签所指的当前版本（emilia v12、mls_sidon v8）
- 核验：清理前枚举统一根全部 manifest 与本次各步 plan，6 处引用均指向当前版本；清理后各剩一个版本，标签、行数不变，当前版本引用的文件全部存在
- 说明：[12](specs/12-selections.md) 规定回收机制实现前生产数据不运行 `cleanup_old_versions`；本次为用户明确决定的一次性例外，回收工具未纳入 pipeline
- 证据：selection-v2 feature run 工作目录的 `final_cleanup.log` 与 `OVERNIGHT.md`
