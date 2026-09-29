# 特征提取前：语言与音频区间核查

核查时间：2026-09-29T11:47:32.652471+08:00（北京时间）。只读检查已发布 v0.1，未启动 codec/speaker embedding 提取。

## 证据范围

- 语言统计：对 16 个固定 Lance snapshot 的 language/duration_seconds 做完整列扫描，全部 134,832,658 条。
- 各语言条数与每份 manifest.languages 完全一致；时长合计与 manifest.duration_seconds 在浮点累计误差内一致。
- 裁剪初查：每个 dataset 按固定 snapshot 行序取首、中、尾等 9 个等间距位置，共 144 条 metadata；每个来源首尾各解码 1 条，共 32 条。
- 32 条完整解码的帧数/采样率均与 base 一致。没有进行音文强制对齐或听检，9 个位置不保证覆盖所有 config/语言/游戏。
- 抽查中 segment_start_frame/end_frame 均为空；这与 adapter 保留原始片段一致，不能仅凭空值证明没有裁剪需求。
- 机器可读证据在 reports/feature-preparation/{language-audit,crop-metadata-probe,missing-language}.json；报告固定 manifest 原文件哈希与 snapshot，行位置只在该 snapshot 内有效。

## 语言总体分布（过滤前）

分组仅为展示/未来训练建议：en-US→en、zh-CN→zh。base 原标签不修改，地区信息保留；中文组不意味着每条都是普通话。

| 语言组 | 条数 | 条数占比 | 小时 | 时长占比 |
| --- | ---: | ---: | ---: | ---: |
| 英语 (en) | 91,253,872 | 67.6794% | 272,114.38 | 72.8283% |
| 中文 (zh) | 24,408,422 | 18.1028% | 57,997.43 | 15.5224% |
| 日语 (ja) | 8,709,295 | 6.4593% | 13,550.05 | 3.6265% |
| 法语 (fr) | 3,418,251 | 2.5352% | 9,878.38 | 2.6438% |
| 德语 (de) | 3,122,950 | 2.3162% | 9,109.86 | 2.4382% |
| 韩语 (ko) | 3,185,391 | 2.3625% | 7,935.41 | 2.1238% |
| 荷兰语 (nl) | 380,457 | 0.2822% | 1,579.76 | 0.4228% |
| 西班牙语 (es) | 225,494 | 0.1672% | 937.68 | 0.2510% |
| 意大利语 (it) | 62,133 | 0.0461% | 257.83 | 0.0690% |
| 葡萄牙语 (pt) | 39,230 | 0.0291% | 168.35 | 0.0451% |
| 波兰语 (pl) | 26,075 | 0.0193% | 107.87 | 0.0289% |
| 缺失 (<missing>) | 1,088 | 0.0008% | 1.04 | 0.0003% |

合计 134,832,658 条、373,638.02 小时。比例分母包含缺失标签；四舍五入后可能不严格加到 100%。

原始标签共有 13 个非空值：de/en/en-US/es/fr/it/ja/ko/nl/pl/pt/zh/zh-CN，另有 null。
en-US 163,274 条 / 271.65 小时来自 genshin_voice；zh-CN 101,054 条 / 156.39 小时来自 starrail_voice。

## 各数据集的标签与时长

| 数据集 | 条数 | 小时 | 发布标签 |
| --- | ---: | ---: | --- |
| aishell3 | 88,035 | 85.62 | zh |
| csemotions | 4,160 | 10.20 | zh |
| emilia | 40,264,231 | 101,655.59 | de, en, fr, ja, ko, zh |
| emilia_yodas | 43,964,905 | 113,820.64 | de, en, fr, ja, ko, zh |
| galgame | 7,101,489 | 10,173.68 | ja |
| genshin_voice | 654,252 | 1,039.72 | null, en-US, ja, ko, zh |
| hifitts | 323,978 | 291.69 | en |
| hifitts2 | 12,809,875 | 35,932.70 | en |
| libriheavy | 12,441,758 | 51,044.65 | en |
| libritts_r | 374,999 | 583.10 | en |
| ljspeech | 13,100 | 23.92 | en |
| mls_sidon | 12,288,862 | 50,834.33 | de, en, es, fr, it, nl, pl, pt |
| starrail_voice | 403,437 | 692.57 | en, ja, ko, zh-CN |
| vctk | 88,328 | 82.65 | en |
| wenetspeech4tts | 3,932,472 | 7,232.15 | zh |
| wutheringwaves | 78,777 | 134.81 | en, ja, ko, zh |

## 标签来源与缺失核查

- Emilia/YODAS：adapter 同时核对语言目录与 upstream.language，固定 DE/EN/FR/JA/KO/ZH→小写标签。
- MLS：8 个语言目录显式映射为 en/de/fr/es/it/pl/nl/pt。
- 原神/星铁：显式映射源 language，Chinese→zh、Chinese(PRC)→zh-CN、English(US)→en-US；空值保留 null。
- 鸣潮：CN/EN/JP/KR archive 与对应语言目录配对，映射为 zh/en/ja/ko。
- 单语数据：AISHELL3/CSEMOTIONS/Wenet4TTS 固定 zh；LibriTTS-R/LibriHeavy/LJSpeech/VCTK/HiFiTTS/HiFiTTS2 固定 en；Galgame 固定 ja。
- 这些检查确认字段映射与来源声明，不证明音频真实语种。单语假设、游戏混语/外来语、字幕与语音不一致仍需独立内容抽查。

全库 null 语言为 1,088 条，均来自 genshin_voice，时长 1.0351 小时。
对这些行逐条检查 upstream.language，全部为原始空串；其中 1,088 条 text 也缺失/空白。
因此按本轮必须有文本的入选规则，这些记录会被排除；不是靠角色或文件名猜语种后补标签。
目前没有修改语言标签，也没有生成后续 LID 标注。

## 逐数据集裁剪决定：初查矩阵

下表是来源语义与抽查证据形成的候选决定，尚不是完整音文对齐验收。除 MLS 已有更广的历史核查外，
“候选整段”仍需按 config/语言/时长分层检查后才能放行全量；未确认范围保持 unresolved。

| 数据集 | 当前证据/候选处理 | 放行前仍需核对 |
| --- | --- | --- |
| aishell3 | content.txt 与单条 WAV 配对；候选整段 | train/test、短长句、文字与拼音列不混用 |
| csemotions | Parquet 一条音频对应一条文本；候选整段 | 情绪类别与长样本的文本覆盖 |
| emilia | 每条 MP3 配独立 JSON，抽样 duration 对应片段；候选整段 | 六语言、录音边界与多说话人例外；不按 rounded duration 裁尾 |
| emilia_yodas | 每条 MP3 配 _id/text，局部 speaker 属于源视频；候选整段 | 六语言、speaker 切换和短/长片段；duration 精度不等于真实帧数 |
| galgame | 每条 audio_ID 对应音频对象；候选整段 | 各游戏、旁白/音效/模板字幕、长片段；缺对齐不能靠固定秒数裁剪 |
| genshin_voice | 每行转写对应游戏音频；候选整段 | 四语言、角色/非对话资产、字幕与完整音频是否一致 |
| hifitts | JSONL audio_filepath 指向 utterance FLAC；候选整段 | clean/other 与各 split；不按上游浮点 duration 切片 |
| hifitts2 | 当前输入为已切分的 22.05k FLAC；候选整段 | text_source=book/mls、各 split、长短边界；不要把原章节下载目录当当前音频 |
| libriheavy | 嵌入音频为独立 clip，text_original 与 ASR 分列；候选整段 | 八 config 的原书文本是否覆盖实际 clip；不能用 ASR 变体暗中替换主文本 |
| libritts_r | utterance ID 与嵌入 WAV、两种文本对应；候选整段 | 各官方 split 与长句；chapter_id 是分组信息，不是切片指令 |
| ljspeech | metadata.csv utterance ID 与 WAV 一一配对；候选整段 | 原文/normalized 语义与端点 |
| mls_sidon | 已切分 FLAC；不得用原录音 begin/end 再裁一次 | 8 语言各 split 的例外；本轮 9 条 end-begin 与本地时长一致 |
| starrail_voice | 独立游戏音频与转写；候选整段 | 四语言、voice_type、非朗读/占位音频 |
| vctk | utterance+mic1/mic2 对应独立 FLAC；候选整段 | 缺转写按过滤规则排除；两个 mic 都需按实际帧数核验 |
| wenetspeech4tts | 独立 WAV 已对应一句/一组文本；文本附带字词时间戳 | 核对时间戳单位/当前 clip 坐标及首尾余量，再决定保留整段或显式 trim view；不能把字词区间直接当必须裁剪的标签 |
| wutheringwaves | 同路径 WAV/LAB 配对；候选整段 | 四语言、Others/Placeholder 类别、LAB 是否覆盖全部发声 |

两个具体例子：

- MLS 抽样：音频只有 17.68 秒，但 metadata.begin_time=395.32、end_time=413.0；这两个数指原录音。
  再对当前音频裁 [395.32,413.0) 会越界。此前逐包首条 2,628 条的核查也支持该解释，见 [MLS 核查](libriheavy-mls-check.md)。
- Wenet4TTS 抽样：4.096 秒 WAV 的转写后包含 [[620,780],...,[3660,3925]]；这些数值看起来是当前片段的毫秒级字词区间，
  目前仅是与时长相容的推断，仍需核对源定义和实际声音；不能把 0–620 ms 自动判为应删除的静音。

## 接下来的放行条件

1. 每个 dataset/config/语言完成分层检查，记录 full_sample / explicit_view / unresolved 与证据；不要按全库一条默认裁剪规则执行。
2. 字幕范围不匹配或边界不明确的样本/子集先列为 unresolved，不自行截断并保留整条文本。
3. 需要裁剪时先明确采样帧坐标及对应文本，以 view 表达；codec 和 speaker 都引用明确音频区间。
4. 过滤缺音频/文本后重新计算语言条数与时长；codec 与 speaker 成功集合再分别统计，训练最终权重另定。
5. 英语占时长 72.83%，中文 15.52%；直接按时长采样会明显偏向英语，不能将当前分布当作已确定的多语言训练配方。
6. 未加载模型、未运行真实 encoder、未启动任何特征提取；这里只整理输入和环境证据。
