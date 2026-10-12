# Selection 文本筛选规则 selection-filter-v1

状态：已实现，规则文件 `configs/selections/filter-v1.json`，执行器 `src/tts_data_pipeline/selection.py`。
用途：一阶段 TTS 预训练的入选集合。基础 `contract_version=v0.1` 不变；原因/flag 字典升为 version 2，见 [12](../specs/12-selections.md)。
目标是排除明确的音文配对问题（错语言、截断/多余文本、ASR 复读、标记垃圾），保留规模和多样性；
不做音质筛选。排除只表示不进入本版训练集合，不删除基础样本，不改写基础或 annotation 文本。

## 输入

- 18 个已发布 base v0.1：旧 16 个，加 zenless_voice、emilia2（ASMR 已在接入时排除）。全部从 base 建立，不以父入选集合为上限。
- 父 selection `tts-selection-supervised-tts-20260929T151539bjt-01`：完整继承其 8 条有效排除（鸣潮一帧音频、Galgame 长音文错配）。
- 不使用音频统计：audio_stats 已停用并删除。时长取 base 的 `num_frames / sample_rate`（接入时实际解码帧数）。
- 不使用 DNSMOS 等质量分数；不做留出评估划分（在最终训练数据构建时处理）。

## 文本来源

| 范围 | 选用文本 | 来源码 |
| --- | --- | --- |
| genshin/starrail/wutheringwaves/zenless | `tts-ann-transcription-20261010T111659bjt-01` 主转写 | 1 |
| emilia | `tts-ann-transcription-20261010T171514bjt-import`（上游 Qwen ASR 导入） | 2 |
| 其余 13 个（含 emilia2 上游 Qwen ASR 文本） | base text | 0 |

选用文本只做首尾去空白。转写 annotation 非 ok（失败/缺失）记 1003，不回退基础字幕。
annotation 按 sample_id 与 base 对齐，并核对 audio_sha256、来源语言及其计算所用 base manifest/版本。
分支只在选用文本与 base text 不同时写 `selected_text`，`selected_text_source` 为上表来源码。

## 语言（严格）

允许 `de/en/es/fr/it/ja/ko/nl/pl/pt/zh`；别名 en-US/en-us→en、zh-CN/zh-cn→zh。不在集合内记 2001/2002。
有转写来源的数据集额外要求：ASR 语言标签经明确映射后恰为一种且等于来源语言；
标签为空、多标签（如 `Japanese,English`）、不一致、`language_mismatch`、曾尝试指定语言候选（含空/失败）均记 2003。
emilia 的语言标签来自上游元数据，与 base 一致属记账核对，不是独立语言识别。其他数据集没有 ASR 证据，只检查允许集合。
全部数据集再检查文字系统（2004）：只计字母，汉字/假名/谚文每字一单位，其他文字每词（同文字连续字母）一单位；
zh 期望汉字，ja 期望汉字+假名，ko 期望谚文，其余期望拉丁字母；期望单位 < 50% 记 2004，无字母不判。
代码混用（如“cubic的系数是a”）保留。主要命中 emilia 上游 ZH 分片中的英语音频（约 216 万条、5,600 小时，base 与新转写都是英文），
以及 emilia2 少量标签与文本不符。只排除，不改写语言标签。

## 文本规则

有效字符：NFC 后 Unicode 类别 L*/N* 的 code point（`str.isalnum`）。

| 原因 | 判定 |
| --- | --- |
| 1001 missing_text | 选用文本为空或有效字符为 0（如只有“………”） |
| 1007 text_markup | 含 `[ ] { } < > \ | ^ #` 任一字符（OCR/排版/标签，如 `<ハ>`、`[Sidenote:…]`、`{\displaystyle`） |
| 1008 asr_chunk_join | ASR 文本含换行：服务端对长音频分块后拼接，断点处文本错位 |
| 1004 text_repetition | 单个 n-gram 出现 ≥5 次且其出现区间并集覆盖 ≥80% |
| 1005 / 1006 | 秒/有效字符 < 下限 / > 上限：zh/yue/ja/ko/th 为 0.04–2.0，其他 0.015–1.0，包含边界 |
| 3001 / 3002 | 时长 < 1 秒 / > 120 秒，包含边界；更细的长度上限由训练端决定 |

复读：casefold+NFC；zh/yue/ja/ko/th 走字符 8-gram（≥30 有效字符），其他语言按空白分词、每词取有效字符后走词 4-gram（≥18 词），
词数不足时回退字符路径；两者都不满足记为未检查（coverage 统计），不声称通过。
只为多行、可复现的系统性模式加规则，不为单条样本加规则。

## 来源时长证据

`reports/interval-audit/findings.tsv`（SHA256 固定在规则文件）中除 `invalid_word_timestamps` 外的 14,250 条记 105：
来源 duration 比实际音频长 0.05–1 秒（emilia 1,416、emilia_yodas 12,814、libriheavy 19、mls_sidon 1），缺口位置未知。
wenetspeech4tts 的 34 条只涉及词级时间戳，完整 WAV 保留。

## 重复

全部 18 个数据集按完整 audio_sha256 一次分组。组内若有成员使用转写 annotation 文本（同一音频 bytes 转写），
只在这些成员间比较文本，其余合格成员为普通非代表（4001）；语言比较覆盖全部合格成员。
仍冲突的组记 4002；否则按来源优先级（旧 16 个原顺序，之后 zenless_voice、emilia2）及 sample_id 选代表。
emilia 与 emilia_yodas 共享约 480 万条相同音频，前者使用新转写，因此 emilia 成员为代表。

## 主原因优先级

103, 102, 104, 105, 1002, 101, 1001, 1003, 2001, 2002, 2003, 2004, 1007, 1008, 1004, 1005, 1006, 3001, 3002, 5001, 4002, 7001, 4001。
全部命中记入 flags（bit 10 来源时间轴，11 文本策略，12 语言证据）。

## 已知未处理

- libriheavy 约 3.8% 文本用 `_斜体_` 下划线标强调，词本身朗读正确，不排除；最终训练数据构建时可去掉下划线。
- mls_sidon 文本全小写无标点，属来源风格，不排除。
- 游戏 ASR 文本有专名同音误写（如“眼狩令”→“演兽令”），预训练可接受；不做逐条修正。
- emilia2 约 1,606 万条（16,244 小时）为允许集合外语言（ar 1,047 万、ru、hi、fa、yue、bn、th 等），按 2002 排除；扩充语种需另定。
- 无音质、近重复、独立语言识别检查；通过规则不等于音文逐字验收。

## 执行

```bash
python scripts/build_selection.py plan --work /tmp/tts-selection-<run> --rules configs/selections/filter-v1.json
python scripts/build_selection.py run --work /tmp/tts-selection-<run> --workers 80
```

plan 固定父 selection、annotation、证据文件与全部 base；scan 前先把 annotation 按 base 行序对齐到本地 parquet。

## 发布结果

`tts-selection-supervised-tts-20261012T045922bjt-01`（2026-10-12 北京时间）：235,079,775 条 / 520,518 小时中入选
204,053,470 条 / 477,713 小时。主要排除：2002 不支持语言 1,606 万（emilia2）、4001 重复非代表 634 万、3001 不足 1 秒 520 万、
2004 文字系统不符 251 万、1007 标记 46 万、2003 语言证据 10 万、4002 冲突重复 10 万。
18 个分支均经独立回读：base main 版本未变、行数一致、原因计数与 manifest 一致、入选行选用文本非空。
检查脚本与报告在 `artifacts/selection-filter-v1-20261012/`。
