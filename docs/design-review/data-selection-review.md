# 数据问题到 selection 的处理边界

本轮只验证分支并更新规范，尚未生成生产selection。以下区分用户提供的全量审计与本轮独立核查，
不把收到的问题数量当成已经执行的排除结果。规则与格式见 [12](../data-contract/specs/12-selections.md)。

| 问题 | 当前证据与处理 |
| --- | --- |
| Emilia/YODAS跨来源重复约541万、全体冗余约610万 | 用户全量审计；已有/tmp哈希数组仅64-bit前缀，正式分组必须读取完整audio_sha256复核。按所有来源统一算，分支分别写原因，重复成员表归本selection。 |
| Emilia约56万、Galgame约6.8万，游戏/MLS/HiFiTTS2内部重复 | 同时纳入全局分组，避免只处理跨来源。代表选择要固定规则；不合并speaker标签。 |
| Galgame/原神文本冲突，原神/星铁语言冲突 | 先规范化比较，再从未被更高优先级排除的候选裁决；未解冲突保守整组排除，保留全部成员/原始差异。差异不直接证明哪条错误。 |
| 鸣潮32条仅1帧 | 已独立只读确认32条num_frames=1；按完整音频hash登记适用范围，不能只写dataset名一刀切。生产排除清单待生成与复核。 |
| Galgame7条超过300秒，文本为“て”或播放器UI提示 | 已只读确认该7条；按sample+text_revision登记已确认配对问题，引用确切证据。不能据此把所有长音频都称为损坏。 |
| Galgame98条<0.1秒；各来源过短/过长 | 用户审计；短音效和长语音须与明确损坏分开。selection采用明确用途时长阈值，保留duration_policy原因；阈值首版发布前定，不改基础校验来销毁原样本。 |
| 原神/星铁缺文本；原神缺语言 | supervised_tts排除缺文本/必要语言，独立audio_features用途可有别的规则；不能把空值当空字符串或unknown语言token悄悄送训练。 |
| en-US/zh-CN别名 | base与speaker_id保持原样；训练选用值按显式alias映射，统计转换前后条数与时长。不是本轮直接重写原标签。 |
| Galgame/Wenet没有speaker | self训练允许；没有身份依据时不能用other_same_speaker配对。 |
| LibriHeavy非48k、长音频 | 非48k本身不是错误；模型前处理按profile重采样，长度上限是selection/模型容量策略，不能偷偷裁剪。 |
| .tmp残留与MLS旧SQLite | 已清理并复核，分别150,705,484,388与3,109,679,104bytes，见published-cleanup-review；不再列作“仍存在”。 |

## 首尾空白专项核查

用户全量扫描记录：Emilia22,001,088条、YODAS43,382,067条text带首尾空白。
本轮每来源6语言、每语言首/中/末3个archive、每个20条，共720条，
逐条比对raw JSON文本、metadata.upstream.text与samples.text，全部一致。
DE/EN/FR/KO各来源各60条均有单个前导ASCII空格；ZH/JA各60条无该现象；
这个样本内没发现尾空格、tab或换行。不是完整文本分布普查，不能用720条推断其他空白类型不存在。

_emilia.checked_metadata只用strip判断是否非空，返回原text，不由pipeline添加空格。
可以确认空白来自上游，不能确认上游为什么保留，也没有证据说明它是训练必需信息。
本轮不修改原文。首版选择/训练按明确unicode-strip-v1解释文字；去重文本比较使用同一规则，
规则版本、实现和受影响数量必须记录。系统性规范化不生成6500万行issue。

## 后续落地

先固定全量输入/完整hash和排除证据 → 生成全局重复裁决与完整reason/flags → 分数据集增列并核验 →
全体输出齐备才发布selection。索引只辅助查询；生产身份/外键/覆盖核验不能用“索引存在”替代。
初版evaluation=not_assigned；没有录音信息的来源不承诺录音隔离，不提前声称评估无泄漏。
annotation/新质量评分不挡此流程，DNSMOS保持上游metadata读取；codec后续按固定selection消费。
