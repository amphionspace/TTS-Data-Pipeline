# 数据集接入表

所有 21 个目录均已盘点。这里区分“有转换规则”与“已实现转换器”。
目前实现 13 个适配器（下表 ✓），新增九个的真实小样本已验证，尚未整库转换。
写入后端为 Lance，release 固定 v0.1；旧产物已清理，全量转换已启动，进度以状态脚本为准。
最新来源复核见 [LibriHeavy / MLS 核查](design-review/libriheavy-mls-check.md)。
Emilia / Emilia-YODAS 的 adapter 已在 `feat/emilia-conversion` 工作树实现，入口为
`reports/runtime/emilia-conversion/scripts/tts_data.py`；主工作树源码仍供原有任务使用，保持冻结。见 [本地核查](design-review/emilia-local-check.md)。
其他适配器按接入优先级逐一补齐，
不创建看起来可运行的空实现。转换不能依赖源数据中旧机器的绝对路径。

| 数据集 / 计划适配器 | 源容器与特殊映射 | 转换前要解决的问题 |
| --- | --- | --- |
| AISHELL-3 / aishell3.py ✓ | tgz；content.txt 的字词与拼音分别保留；speaker 来自源 ID | 完整清单与音频对应关系、文本解析验证 |
| CSEMOTIONS / csemotions.py ✓ | Parquet；保留 emotion；speaker 加数据集命名空间 | 缺上游 ID，以源文件哈希固定快照；完整转换结果见首批报告 |
| Emilia / emilia.py | 分语言 tar；JSON+MP3；保留 dnsmos、源 speaker | 原 JSON wav 路径与实际 member 配对；说话人 ID 范围 |
| Emilia-YODAS / emilia_yodas.py | 分语言 tar；JSON+MP3；支持 _id、phone_count | speaker 通常受视频范围约束；不能仅凭末尾编号跨视频合并 |
| Galgame / galgame.py ✓ | 各游戏配置的 Parquet；audio_ID、text | 此前缺的 6 个 shard 已补齐；无 speaker 列；源配置音频采样率声明需实测 |
| HiFiTTS / hifitts.py ✓ | tar.gz 内多个 JSONL manifest；保留原文/规范化文本与 clean/other | manifest 全量读取与音频成员对应关系 |
| HiFiTTS2 / hifitts2.py | 章节 MP3、少量章节/切分元数据 | 当前 URL 清单缺 2,454 个文件；缺完整训练文本/切分标注 |
| LJSpeech / ljspeech.py ✓ | tar.bz2；metadata.csv 的 id、原文、规范化文本 | 全量文本与 wav 配对、官方 split 不存在时显式定义构建 split |
| LibriTTS-R / libritts_r.py ✓ | Parquet；源 id、chapter、speaker；两种文本 | 忽略旧绝对 path；完整转换全部配置，完成状态及逐配置统计见首批转换报告 |
| VCTK / vctk.py ✓ | zip；txt 与 mic1/mic2 FLAC | 两个麦克风为同语句关联样本；防 split 泄漏，检查缺文本情况 |
| WenetSpeech-Chuan / wenetspeech_chuan.py | tar、text.jsonl、Lhotse manifests | 替换旧 tar command 路径；长音频、文本粒度与 speaker 缺失 |
| WenetSpeech-Wu / wenetspeech_wu.py | tar、JSONL；transcription 与 translation 分开 | 吴语转写不能被普通话翻译替代；保留 DNSMOS/SNR/confidence |
| WenetSpeech-Yue / wenetspeech_yue.py | tar、audio_index.tsv、Lhotse manifests | 通过 index 定位真实 member；all/clean/eval 重叠；多说话人标记 |
| WenetSpeech4TTS / wenetspeech4tts.py ✓ | tier tar.gz、filelists、DNSMOS 清单 | 三份完整 filelist 集合关系核对通过；全量读取 70 个物理包一次；完整 archive 对账待全量 |
| WutheringWaves-2.2 / wutheringwaves.py ✓ | 4 个语言 7z；wav+lab、角色目录 | 四语言完整成员配对、各 4 条真实解码通过；保留类别/占位文本；全包验收未完成 |
| dns5 / dns5.py | 噪声/RIR 压缩包 | 作为增强资源单独管理，不混入有监督 TTS target；需独立 asset schema |
| genshin-voice / genshin_voice.py ✓ | Parquet；transcription、language、speaker、inGameFilename | 空文本/说话人/语言；角色不是跨语种共享音色；字幕真实性待核 |
| libriheavy / libriheavy.py ✓（Lance v0.1） | Parquet；原书文本与 ASR 分开 | 全部 12,441,834 个 ID 已核对，八配置无 ID 交集；原始配置进 metadata，统一输出 train |
| mls_sidon / mls_sidon.py ✓（Lance v0.1） | 8 语言 tar.gz；FLAC+metadata.json | 原 HF paths.yaml 与本地完全相同，2,628 包逐包首条配对通过；实际 FLAC 路径与源秒数分别保留 |
| starrail-voice / starrail_voice.py ✓ | Parquet；ingame_filename、transcription | 空文本/说话人；保留 voice_type；先区分对话与其他声音 |
| zenless-voice / zenless.py | Parquet；ingame_filename、transcription | 空文本/说话人、性别分支/变量标签、字幕可能不是实际朗读内容 |

## 新增适配器

1. `adapters/<dataset>.py` 提供 `iter_records(root, snapshot)`，逐条产出统一记录。
2. 字段映射、source_key 的唯一性范围、source_split、语言和说话人语义在本文件记录。
3. 通用格式逻辑调用 schema/writer，不修改原始数据；来源特有字段放 metadata 或 text_variants。
4. 至少验证真实样本、未知字段为 null、原 bytes 不变、身份无冲突、旧路径不被当成本机路径。
5. 完成完整遍历与拒绝记录输出后才能标为“可全量转换”；通过预览不等于完成整库验收。

本轮九个接入的具体映射和验证边界见 [适配与契约复核](design-review/adapter-contract-review.md)。

## LibriHeavy 与 MLS SIDON 映射

- LibriHeavy：保留源 `id`；`text_original` 为主文本（source_book_text），
  `text_transcription` 放 source_asr 变体。书籍 ID 用作 group_id；不从切片文件名猜章节。
  speaker 在数据集内命名。各 HF config 的原始 split 本来就是 train；
  metadata 同时保存 original_config 与 original_split。单配置入口默认 small；bulk 入口独立保留全部配置，最终审计 source_key 交集，
  训练构建按明确的配置选择和去重策略使用。
- MLS SIDON：按 tar 内同 stem 的 `.flac` / `.metadata.json` 配对，允许任意先后；
  缺配对、重复成员、ID 不匹配会报错。只流式读取，不解压到原始目录；未匹配缓冲上限 64 MiB。
  以语言 + 源 id 定义来源键；speaker 按数据集 + 语言隔离，暂不跨语言推断同一个人。
  original_path 的摘要标识原录音并用于 group_id；全部原 JSON 保存在 metadata.upstream。
  begin_time/end_time 保留为来源秒数，不假定恢复音频与原录音具有逐帧对应关系。
  音频名称以实际 FLAC member 为准，元数据的旧 `.opus` 文件名只用于溯源。
  `dev` 文件对应数据卡的 `valid` split；两者分别保存在 original_archive_split 与 original_split。
- 所有输出 source_split=train，音频编码 bytes 不改变；语言使用 en/de/fr/es/it/pl/nl/pt。
- 样本验收各取 16 条：LibriHeavy 8 个配置，MLS 8 语言 × train/dev/test，共 512 条。
  这是源文件前缀覆盖，不是随机质量抽样，也不检查整包尾部完整性、全库 ID 或配置交集。
  上述为早期验收范围；新增完整 ID 和逐包首条复核见本文开头的最新报告。当前运行状态见 scripts/conversion_status.py。

## 本轮九个 adapter 的固定映射

| CLI dataset_id | raw 目录 | 配对、身份和关键语义 |
| --- | --- | --- |
| aishell3 | AISHELL-3 | train/test 的 content.txt 与完整 WAV member 配对；来源键 split/filename；文字与拼音分列；speaker 为 SSB 前缀；原词/拼音序列保留 |
| ljspeech | LJSpeech | metadata.csv 与完整 wav 路径配对；来源键 LJ utterance ID；原文为 text，normalized 为变体；单 speaker LJ |
| vctk | VCTK | mic1/mic2 分别保存，来源键 utterance+mic，共用 utterance group；没有 txt 的音频保留 text=null |
| hifitts | HiFiTTS | 全部 clean/other × train/dev/test manifest 按完整 audio_filepath 配对；原文与预处理/规范化变体保留；按 book 分组 |
| wenetspeech4tts | WenetSpeech4TTS | all/Basic 读三个物理目录共 70 包一次，Standard 读 Standard+Premium，Premium 只读 Premium；来源键 utterance ID；无可靠 speaker，不推断 |
| genshin_voice | genshin-voice | 来源键相对 shard+原始行号；角色按 dataset+language 命名；保留全部小字段与原音频路径 |
| starrail_voice | starrail-voice | 同上；保留 voice_type、ingame_filename、缺失文本与角色原值 |
| galgame | Galgame-VisualNovel-Reupload | all 读全部游戏，也可 --config 游戏目录；来源键 shard+原始行号；保留 audio_ID/game；speaker=null，game 仅作 group |

全部基础 source_split=train；已有上游划分保存 original_split，无官方划分用 null。
游戏语言映射：Chinese→zh、Chinese(PRC)→zh-CN、English→en、English(US)→en-US、Japanese→ja、Korean→ko；
空语言→null，同时不生成缺少语言范围的角色 speaker_id。保留未经清洗的原文，未知新语言标签报错。
角色标签不等于经过验证的声纹身份；参考配对仍需标注或明确的来源可靠性策略。
Galgame 数据卡的 48k 声明不能代替实际测量，真实预览为 44.1k，按实际头信息保存。

Wenet 的 Basic_filelist.lst、Basic_DNSMOS.lst 是每个 archive 的语义依赖，参与 source-unit-v1 快照；
保留 upstream_dnsmos_p808 原始分数和完整 transcript 文件内容，不冒充已完成质量标注 run。
其他适配器自包含于单个 archive/Parquet，继续 source-file-v1。

流式 tar 用磁盘 SQLite + 单个音频 spool 配对，不要求音频与文本相邻。
音频在文本前时，每个 worker 的临时空间最坏接近解压后整包音频大小；通过 TMPDIR 选择有空间的本地磁盘。
Wenet 包约 9GB 压缩输入，不能直接按 64 worker 而忽略解压空间。单个大 archive 当前只用一个 worker；
LJSpeech/AISHELL/HiFiTTS 的单包全量速度需单独测，不能用增加 worker 数声称单包会变快。
Wenet 每包当前扫描共享清单，安全哈希校验也有额外开销；全量前可增加固定依赖哈希的只读索引缓存。

鸣潮入口 wutheringwaves：支持 all/CN/EN/JP/KR；来源键为完整 WAV member，LAB 严格同路径配对。
原角色、类别路径（Others/Placeholder）和未清洗 LAB 文本保留；None、unknown、纯问号标签转空 speaker。
已知角色按数据集+语言命名，角色标签仍非声纹确认。四个语言均完成真实预览。
7z 通过 [py7zr 自定义 WriterFactory](https://py7zr.readthedocs.io/en/latest/api.html) 解压到单个临时 spool，
按成员范围读取，解压 CRC/字节长度错误会失败；预览只提取选定配对，完整转换每包顺序解压一次。
完整转换单包需约 10–11.4 GB 解压临时空间，最多四包可独立并行；目录不写回 raw。
