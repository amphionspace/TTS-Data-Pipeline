# 新来源适配与 v0.1 契约复核 · 2026-09-28

新增九个 adapter，registry 现有 13 个；CLI preview/convert/bulk 均已接入。
本轮只做接入、测试与真实小样本验收，未启动全量转换。原数据未修改，Emilia2 继续延后。

## 来源检查

| 来源 | 本轮已完成的检查 | 仍未覆盖 |
| --- | --- | --- |
| AISHELL-3 | 实际 content.txt/音频/说话人格式；文字拼音解析；4 条真实回读解码 | 完整 archive 遍历、prosody 的结构化导入 |
| LJSpeech | 原文/normalized 配对；4 条真实回读解码 | 全量音频和包尾 |
| VCTK | ZIP 全成员与文本关系：88,328 音频，44,583 transcript ID；172 音频缺文本（p315），300 文本无对应音频；4 条真实解码 | 全量音频解码；文字无音频的条目不生成假音频行 |
| HiFiTTS | 内嵌 JSONL 清单和完整路径；4 条真实回读解码 | 全部 manifest/音频的最终数量对账 |
| WenetSpeech4TTS | 三份完整 filelist 集合核对；三个物理 tier 各 4 条真实回读解码，保留原始 DNSMOS | 70 个 archive 完整遍历及音频解码 |
| genshin-voice | 75 片全部小字段扫描，共 654,252 行；4 条真实回读解码 | 全库音频解码、字幕匹配及角色真实性 |
| starrail-voice | 47 片全部小字段扫描，共 403,437 行；4 条真实回读解码 | 同上 |
| Galgame | 1,230 片全部小字段扫描，共 7,102,830 行；4 条真实回读解码 | 全库音频；speaker 后续标注 |
| WutheringWaves-2.2 | CN/EN/JP/KR 四包完整成员配对；每语言 4 条真实回读解码；None/纯问号角色为空 | 全部 solid archive 完整解压与音频解码 |

累计 14 次预览、56 条真实音频，全数验证 base schema、完整波形解码、Lance 回读和原始 bytes 一致。
这是有界前缀覆盖，不是随机质量抽样，也不是 9 个数据集整库验收。
详细证据位于忽略 Git 的 reports/adapter-review/acceptance.json、game-scalars.json、wenet-membership.json。
预览用人工 snapshot 标记，不能作为正式来源指纹发布；正式 convert 才读取完整来源哈希。
临时 Lance 预览已在保存证据后清理，不作为 unified 数据发布。

### Wenet 的集合关系

完整清单：Basic 3,932,473；Standard 1,941,220；Premium 407,494。
物理分区：Basic 独有 1,991,253；Standard 独有 1,533,726；Premium 407,494。
Basic ⊃ Standard ⊃ Premium，清单路径对应物理分区，发现 0 个不一致。
该核查覆盖全部清单 ID/路径，不声称验证了所有 archive 内容；adapter 完整转换时会双向对账。
与[上游数据卡](https://huggingface.co/datasets/Wenetspeech4TTS/WenetSpeech4TTS/blob/main/README.md)描述一致。

### 游戏数据的缺失情况

原神：52,693 条缺文本、7,291 条缺 speaker、1,088 条缺语言。
星铁：61,375 条缺文本、60,164 条缺 speaker。
Galgame：当前 text/audio_ID 小字段扫描未发现空值，但源 schema 不提供 speaker。
保留全部真实音频，缺值写 null；不填假字幕/假 speaker，不直接把角色目录当成经过声纹验证的同人。
Chinese 与 Chinese(PRC)、English 与 English(US) 均有实际出现，固定映射见接入表。

## 已修正的契约问题

1. 固定 source_split=train；校验 text_variants 结构及非有限 JSON，禁止未定义的 NaN/Infinity。
2. record_revision 仅覆盖固定基础列，新增标注列不改变基础修订；输入验证仍要求显式投影基础列。
3. 自包含来源继续 source-file-v1；外部清单/评分引入 source-unit-v1，排序后的完整依赖参与身份、manifest 和恢复检查。
4. 一对多标注增加 targets.lance：成功零事件与失败、未运行可区分；Arrow 描述同步导出。
5. 外部表/manifest 引用相对 unified 根，产物自身表路径相对 release/build；snapshot 必须是明确的正整数。
6. release_dataset 拒绝缺失 snapshot 和 incomplete 目录，避免无意读取 latest。
7. 质量列 manifest 区分结果 rows、整表 table_rows 与 selection 的任务范围；例子计数一致。
8. recipe 含 sampling，改变策略必须产生新 build_id；复用 codes 不得偷偷改变物化/索引引用布局。
9. num_frames 明确为读取器报告值；view/codec 有效时间轴另行验证，不能把头信息称为 padding 已校正。
10. 明确授权的整文件排除写入 manifest；未知新坏文件仍失败。sample_id 是 64 个 hex 字符，不是 64 bit。
11. 发布软件依赖记录 Python、libsndfile、py7zr、PyYAML 及 Arrow/Lance/NumPy/SoundFile 版本。

规范源稿 21 个文件完成类型、JSON/YAML、示例计数与内部链接检查；部署副本逐文件 byte compare。
复核命令：`python scripts/check_contract.py --target /workspace/data/DATA-TTS-UNIFIED`。
规范与实现边界仍分开：标注协调发布、codec 生成、训练 build/sampler 的生产执行器尚未完成。

## 测试与资源边界

清理后保留 68 项回归测试；移除与当前布局不符的 codec 主表实验，保留标注状态、
稀疏关联、固定快照、原音频不重写与索引查询检查。部署的 21 份规范与源稿逐字节一致。

测试覆盖音频在文本前/后、缺配对、重复/错误路径、多 mic、缺文本/语言/角色、7z CRC/长度读取路径、
外部评分变化导致身份失效，以及 bulk 检查点的依赖变化拒绝。
测试夹具的完整转换通过，不替代真实全库完整性验收。

流式 tar 配对使用 SQLite 与音频 spool，7z 使用一个按成员范围分配的 spool；均在受控临时目录自动清理。
Wenet 音频先于文本，真实前缀预览约 73–80 秒（需顺序读过音频区）；不能把这个数字当训练吞吐。
鸣潮预览只提取少量配对，四语言并行各约 4 秒；完整转换每包约 10–11.4 GB 临时解压空间。
单包任务仍是一个 worker，多 worker 不能加速单个 tar。全量吞吐与最大临时占用需另测。

## 原四个来源的 Lance 验收

| 来源 | 覆盖 | 通过行数 |
| --- | --- | ---: |
| CSEMOTIONS | 一个完整源 Parquet，回读、完整解码、来源哈希复核 | 520 |
| LibriTTS-R | test.other 一个完整源 Parquet，同上 | 1,707 |
| LibriHeavy | test_clean 一个完整源 Parquet，同上 | 2,557 |
| MLS SIDON | french/dev 首 16 条，原 bytes 回读和完整解码 | 16 |

共 4,800 条，前三项还验证了 sample_id ScalarIndexQuery 和音频读取。
MLS 为有界预览，不是正式来源快照；这些结果不代表整库验收或生产吞吐。
原始验收证据保留于 reports/lance-v0.1/source-acceptance.json。
