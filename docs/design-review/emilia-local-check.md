# Emilia / Emilia-YODAS 本地接入核查

核查日期：2026-09-28。范围仅为 `/workspace/data/DATA-TTS/Emilia` 和
`/workspace/data/DATA-TTS/Emilia-YODAS`，不包含 Emilia2。
结论：可以按现有 Lance v0.1 基础 schema 接入，两者保留独立 dataset_id。
本次只核查数据和验证映射，尚未实现两个 adapter，也没有启动转换或改写原始数据。

## 本地清单

大小为十进制 GB；不据此推断音频小时数或总样本数。

| 语言 | Emilia tar 数 | GB | Emilia-YODAS tar 数 | GB |
| --- | ---: | ---: | ---: | ---: |
| DE | 90 | 37.581 | 161 | 167.492 |
| EN | 1,140 | 1,156.142 | 1,362 | 1,489.018 |
| FR | 100 | 62.721 | 213 | 222.139 |
| JA | 70 | 84.544 | 30 | 30.929 |
| KO | 40 | 5.042 | 208 | 217.628 |
| ZH | 920 | 1,264.476 | 9 | 8.545 |
| 合计 | 2,360 | 2,610.507 | 1,983 | 2,135.750 |

两者合计 4,343 个未压缩 tar，约 4.746 TB。各语言文件编号从 0 连续到末尾，
本次目录中未见非 tar 文件。连续编号不等于与上游权威清单逐项核对。

## 已完成的检查与边界

- 全部 4,343 包：读取第一组同 stem 的 JSON/MP3，检查 ID 与 member、目录语言与 JSON
  language、Emilia wav 字段的 basename、音频头和正帧数，全部通过。
- 全部包：大小为 512 字节倍数，最后 1,024 字节为零；这是外围结构检查，不能证明
  tar 内部没有缺失、损坏、重复或提前出现的结束块。
- 每个数据集 × 6 语言 × 首/中/末包 × 前 8 条，共 288 条完整 MP3 解码：
  波形值有限、解码帧数与 SoundFile 头信息一致。属于分层前缀抽查，不是随机质量抽样。
- 对上述样本及额外 44.1 kHz / 时长差异样本，共 292 条，通过现有 27 列 make_record/validate_record
  与完整解码验证；所有实测头帧数与解码帧数一致。
  只在内存中映射，review-only 快照标记不用于生产 ID；未伪造完整源文件哈希。
- 未做 4.746 TB 全量 SHA256、所有 tar 成员遍历、全量 JSON 统计、全量解码、
  全库 ID/音频去重或音文一致性、声纹身份核验。正式转换仍需完整配对、EOF、源哈希、
  写后回读、全局 ID 唯一性及行数对账，发现未知错误仍失败。

逐包首条中没有空 text/speaker、ID 不匹配或重复 ID；不能外推为全库都没有。
原始检查证据在忽略 Git 的 `reports/emilia-review/`，包括清单、逐包首条、解码与映射结果。

## 字段与接入映射

| 项目 | Emilia | Emilia-YODAS |
| --- | --- | --- |
| dataset_id / adapter | emilia / emilia.py | emilia_yodas / emilia_yodas.py |
| 原字段 | id, wav, text, duration, speaker, language, dnsmos | _id, text, duration, speaker, language, dnsmos, phone_count |
| source_key | 原 id；必须等于实际 member stem | 原 _id；必须等于实际 member stem |
| 身份 | source-file-v1，实际 tar 相对路径 + 完整 SHA256 固定来源 | 同左 |
| source_config / language | 保留 DE/EN/FR/JA/KO/ZH；语言映射为 de/en/fr/ja/ko/zh | 同左 |
| source_split | train；本地未提供原 split，metadata.original_split=null | 同左 |
| text | 原 text，text_kind=source_transcript，不擅自清洗或宣称人工转写 | 同左 |
| audio | 原 MP3 bytes，实际 member 定位；不按 JSON wav 旧路径打开文件 | 原 MP3 bytes，实际 member 定位 |
| 时长/帧数/采样率 | 从实际音频测量；metadata.upstream.duration 保留源声明 | 同左 |
| 原评分及扩展 | 完整 JSON 放 metadata.upstream，保留 dnsmos | 同时保留 phone_count；它不是音素序列或 codec token 数 |

Emilia 的 JSON wav 示例是 `EN_B00000/EN_B00000_S00000/mp3/...mp3`，
实际 tar member 在根层；只能按真实 member 配对，原 wav 字符串作来源信息。
不能假定成员永远相邻，不能从 tar 文件名的 B 编号替代 speaker 原值。

两个 adapter 可共享严格的 JSON/MP3 tar 配对工具；分别固定来源映射。
未压缩 tar 适合流式读取并按包并行，不需要先解压成海量小文件。
复用有界内存及本地临时区的配对实现；以实际压测决定 worker 数，考虑当前已有任务的 I/O。
约 1 GiB Lance fragment 与最终单表目录沿用现有约定，调度批次不暴露为公开目录。

## 音色克隆的 speaker 范围

Emilia 首条标签形如 `EN_B00000_S00000`；保留完整标签并加 `emilia:` 命名空间，
不能只保留末尾 S00000。现有本地字段不足以证实跨标签属于同一个人，也不足以把 B 当作
原视频/录音 ID。因此 recording_id=null；可用完整来源 speaker 标签分组防止该标签跨训练/评估。
speaker_scope 标识来源标签范围，不把它当作声纹确认结果。

YODAS 首条标签形如 `EN_tKvmUvxYZXI_SPEAKER_00`，_id 形如
`EN_tKvmUvxYZXI_W000006`。保留完整 speaker；提取共同来源前缀作为录音分组，
recording_id/group_id 包含 dataset + language + 11 字符来源键。
解析应锚定末尾 `_W数字` / `_SPEAKER_数字`，来源键自身可能包含 `_` 或 `-`。
不跨来源键合并 SPEAKER_00，不跨两个 dataset 自动合并同人。
本地命名支持这种保守分组，但没有逐条外部视频身份核验；不能据此声称音色已验证。

训练阶段可在经过筛选的同一局部 speaker 组内挑选不同 utterance 做参考/目标，
仍需要时长、音质、音文与声纹一致性检查；不要把同一条音频同时作为参考和目标。

## 实际发现的差异

1. Emilia 每包首条采样率：24 kHz 1,377 包、32 kHz 887 包、44.1 kHz 96 包，均为单声道。
   YODAS 每包首条均为 24 kHz 单声道。基础层保留实测值；codec 构建再按 profile 重采样。
2. 原 duration 不是逐帧真值。逐包首条最大绝对差：Emilia 约 0.984 ms，YODAS 约 63.208 ms。
   288 条分层解码样本头帧数与实际完整解码一致；源声明差异不应改变基础音频或伪造片段边界。
3. 语言标签存在粒度/一致性问题：YODAS `JA/JA-B000029.tar` 的抽样文本为英文而 language=ja；
   `ZH/ZH-B000008.tar` 有粤语文本而 language=zh。这是文本与标签核查，未据此判断实际语音语言。
   接入保留原标签，后续语言识别/方言与音文一致性标注再决定训练语言及筛选，不凭文字自动改标签。
4. 每包首条 dnsmos 均为 float：Emilia 范围 3.0003–3.6165，YODAS 2.4081–3.5747。
   这是首条样本范围，不是全库分布。局部低于 3 的 YODAS 样本不自动删除。
   不把 dnsmos 当作已校准的通用 quality，也不能从它推断模型版本或与其他来源分数可比。
   接入方案按用户要求将原评分结构化导入同一 samples.lance 的版本化标注列，
   原始 JSON 同时保留；导入注明 upstream / unknown_version。
   缺分数保留 null，训练配方明确缺值和阈值策略。

## DNSMOS 同表保存（用户确认）

每个 dataset 的 `samples.lance` 保存结构化列 `ann__dnsmos__upstream_v1`，
其类型沿用 v0.1 annotation struct，`result.score` 为 float64。
有分数的值形如 `{status:ok, input_fingerprint:..., error_code:null, result:{score:3.2927}}`。
无上游分数的行整列为 null；非法分数必须显式失败/报告，不能静默补 0。

`upstream_v1` 是本次导入 run 的版本，不是 DNSMOS 模型版本；run/profile manifest
注明是上游已有评分、来源字段 dnsmos、模型/实现版本 unknown_version。
输入指纹固定导入所依据的原始记录/音频；它证明导入对应关系，不代表我们重新算过该分数。
Emilia 与 Emilia-YODAS 在各自数据集下固定 profile/run，不能凭相同字段名假定评分实现相同。

基础 27 列写入和验证后，通过标注发布阶段在同表追加该列并固定新 snapshot；
不把标注列硬塞进当前要求恰好 27 列的 base ingest。
训练读取包含该列的 snapshot，仅投影 `ann__dnsmos__upstream_v1.result.score` 等小列即可筛选/抽样，
不用训练时解析 metadata JSON，也不把分数存成另一份复制音频的数据集。
以后重新计算时新增独立 run 列，不覆盖上游值。这个设计符合已有 contract，
但通用标注发布执行器目前未实现；两个 adapter 接入时需一并实现、验收这次上游评分导入。

## 下一步

按上述规则实现两个薄 adapter 和共享配对读取器；先完成小包/真实多语言转换与 Lance 回读，
核对原 bytes、身份、speaker 范围、原元数据及缺失值，再开始全量转换。
本次核查没有发现需要修改 v0.1 基础格式的阻塞；完整性及训练质量仍需后续各阶段验收。
