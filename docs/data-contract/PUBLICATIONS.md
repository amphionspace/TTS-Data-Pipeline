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
| 基础数据集 | emilia/v0.1 | 40,264,231 条，101,655.6 小时 | 2026-09-28 23:17 | |
| 基础数据集 | emilia2/v0.1 | 99,840,397 条，146,325.0 小时 | 2026-10-12 03:10 | |
| 基础数据集 | emilia_yodas/v0.1 | 43,964,905 条，113,820.6 小时 | 2026-09-29 10:30 | |
| 基础数据集 | galgame/v0.1 | 7,101,489 条，10,173.7 小时 | 2026-09-28 18:43 | |
| 基础数据集 | genshin_voice/v0.1 | 654,252 条，1,039.7 小时 | 2026-09-28 19:00 | |
| 基础数据集 | hifitts/v0.1 | 323,978 条，291.7 小时 | 2026-09-29 00:40 | |
| 基础数据集 | hifitts2/v0.1 | 12,809,875 条，35,932.7 小时 | 2026-09-29 11:22 | |
| 基础数据集 | libriheavy/v0.1 | 12,441,758 条，51,044.7 小时 | 2026-09-29 10:27 | |
| 基础数据集 | libritts_r/v0.1 | 374,999 条，583.1 小时 | 2026-09-28 17:39 | |
| 基础数据集 | ljspeech/v0.1 | 13,100 条，23.9 小时 | 2026-09-28 16:44 | |
| 基础数据集 | mls_sidon/v0.1 | 12,288,862 条，50,834.3 小时 | 2026-09-29 04:23 | |
| 基础数据集 | starrail_voice/v0.1 | 403,437 条，692.6 小时 | 2026-09-28 19:00 | |
| 基础数据集 | vctk/v0.1 | 88,328 条，82.7 小时 | 2026-09-28 17:46 | |
| 基础数据集 | wenetspeech4tts/v0.1 | 3,932,472 条，7,232.2 小时 | 2026-09-28 20:10 | |
| 基础数据集 | wutheringwaves/v0.1 | 78,777 条，134.8 小时 | 2026-09-28 17:45 | |
| 基础数据集 | zenless_voice/v0.1 | 406,720 条，554.4 小时 | 2026-10-09 18:53 | |
| Annotation | `tts-ann-transcription-20261010T111659bjt-01` | transcription；genshin_voice、starrail_voice、wutheringwaves、zenless_voice；1,543,186 行 | 2026-10-10 11:17 | selection-v2 文本来源 |
| Annotation | `tts-ann-transcription-20261012T092945bjt-01` | transcription；emilia；40,264,231 行 | 2026-10-12 09:39 | selection-v2 文本来源 |
| Annotation | `tts-ann-upstream_quality-20261013T143341bjt-01` | upstream_quality（upstream-dnsmos-p835-ovrl-v1）；emilia、emilia_yodas、emilia2；184,069,533 行 | 2026-10-13 16:38 | |
| Selection | `tts-selection-supervised-tts-20260929T151539bjt-01` | selection-v1；16 个数据集；入选 128,220,178 条，357,506.8 小时 | 2026-09-29 15:39 | 现有训练 build 使用 |
| Selection | `tts-selection-supervised-tts-20261012T123927bjt-01` | selection-v2；18 个数据集；入选 203,998,904 条，477,595.7 小时；上游测试集留出 | 2026-10-12 13:31 | 当前一阶段预训练 |
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
