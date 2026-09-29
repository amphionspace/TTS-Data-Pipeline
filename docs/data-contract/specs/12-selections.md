# 12 · Selection：基础表的可复现选择分支

## 目的与边界

selection 是一次完整的数据决策发布：包含所有输入数据集、全部样本的入选状态、多重原因、
已确认排除证据、重复关系、规范化规则和检查覆盖。它不是单个过滤表达式，也不是另一份音频目录。
查询“这条为什么没选”“和哪些样本重复”“这次哪些可训练”均有固定结果。
原始 27 列、音频、text、language、speaker_id 和基础 manifest 保持不变。

使用同一 samples.lance 内的 Lance branch。每行保留，增列记录状态；reason=0 的逻辑视图才是入选集合。
不用 delete 表示业务禁用，不另存全量行偏移数组或位图，不建立 catalog/curation/_shared 层。
annotation布局与依赖见05；本章的规则结果不等于完成音质或音文全量验收。
本次无annotation输入时manifest显式annotation_inputs=[]，不为启动selection强制先导入质量分数。

## 目录、身份与固定输入

```text
selections/<selection_id>/
├── manifest.json
├── rules.json
├── exclusions.jsonl              # 自包含的有效排除条目（可为空）
├── exclusion_changes.jsonl       # 本次新增、撤销的依据（可为空）
└── duplicates.lance/             # 只保存重复组成员；无重复时 manifest 显式为空

datasets/<dataset_id>/v0.1/samples.lance/
└── tree/<branch_name>/...        # 引擎管理，不手工操作
```

selection_id 格式 `tts-selection-<purpose>-YYYYMMDDTHHMMSSbjt-NN`，北京时间、正整数序号；
purpose 首版 supervised-tts。规则身份用 rules.json 原始文件 SHA256，不能以时间或名字替代。
branch_name 使用 selection_id；manifest 引用原 samples.lance 的根路径、分支名、整数版本三元组。
一个selection_id对应每个输入dataset/release的一条分支，并非全库共用一张分支表。
每条输出通过(table_path,branch,lance_version)唯一定位；同名branch及相同版本号不代表同一张表。
目录名、manifest.selection_id和output.branch保持一致；消费者仍读取显式绑定，不靠拼路径或latest猜测。
修改输入或规则创建新selection_id，中断恢复沿用原ID和输入；发布器原子占用该ID，拒绝其他执行混用。
分支的版本号只在该分支内有意义；禁止只传 version 或使用 branch latest。
base、selection、feature、build 的引用路径均相对统一根，见 10；不得依赖调用者当前目录。

每个输入固定 dataset_id、release_id、基础 manifest 路径/原文件 SHA256、base main 整数版本、行数。
每个输出固定同一 dataset/release 的 branch/version、tag、总行数、selected_rows、原因计数和校验结果。
manifest 还包括 rules_sha256、规则实现摘要、reason/flag 字典版本与定义、执行检查及覆盖、语言/时长统计、
排除文件路径/哈希、重复表版本/类型/行数、父 selection 审计引用，以及 finished_at（+08:00）。
annotation_inputs固定实际采用的任务manifest哈希、dataset/release、table/branch/version/column及输入bv；
无输入写空列表。text_sources按整数码固定文本修订来源，规则明确优先级、未处理/失败/不支持/跳过策略。
selection仍从base建立，绝不以annotation分支为父。采样权重属于training_plan，不默认固化在selection。
示例见 [selection manifest](../examples/selection-manifest.example.json)。

## 分支列和原因字典

| 列 | 类型 | 规则 |
| --- | --- | --- |
| selection_reason | uint16，业务非 null | 0 入选；非零为按显式优先级选出的主原因 |
| selection_flags | uint32，业务非 null | 所有命中检查类别的位掩码；保留重复代表等非排除信息 |

二者全量未压缩约 0.809 GB（134,832,658 行 × 6 bytes），另有 Lance 元数据、索引和稀疏重复表。
不复制基础音频/文本。实际大小依赖编码和分布；不能用高度重复的合成列压缩率预测生产占用。
完整分支总行数等于 base；禁止通过过滤写分支导致失去未入选行的解释。

初始原因注册表（空档保留；已分配码永久不改义、不复用）：

| 码 | 名称 | 含义 |
| --- | --- | --- |
| 0 | selected | 通过此次用途要求的检查 |
| 101 | missing_audio | 无可用音频 |
| 102 | decode_failed | 已执行解码且失败 |
| 103 | invalid_audio | 已确认无效音频，例如 1 帧完整台词 |
| 104 | invalid_timeline | 原生帧区间或解码长度不一致 |
| 1001 | missing_text | 文本为空或按固定空白判定规则为空 |
| 1002 | confirmed_mismatch | 已确认文本/音频不匹配 |
| 2001 | missing_language | 该用途要求语言而缺失 |
| 2002 | unsupported_language | 规范化后不在该用途的语言集合内 |
| 3001 | below_duration_policy | 小于本次明确的时长阈值，不自动称为坏音频 |
| 3002 | above_duration_policy | 大于本次明确的时长阈值 |
| 4001 | duplicate_not_selected | 无冲突重复组中的非代表成员 |
| 4002 | conflicting_duplicate | 文本或语言仍冲突，未决成员保守排除 |
| 5001 | reference_identity_unavailable | 此参考协议要求身份但不能验证；self 不应用 |
| 7001 | held_out | 有明确评估划分时不属于训练侧 |
| 65535 | pending | 仅未完成计算内部使用，complete 中禁止出现 |

6000–6999 为后续质量策略保留，其他未分配码不得擅用；需要更多原因可正式扩展字典。
主原因优先级固定为：已确认音频/配对问题 → 缺音频/文本 → 用途要求的语言/时长/参考规则 →
未解决重复冲突 → 留出评估 → 普通重复非代表。rules 必须枚举具体有序码列表，不通过码值大小推断。

flags 独立分配 bit：0 音频问题，1 缺文本，2 配对问题，3 语言规则，4 时长规则，
5 重复成员，6 未解决重复冲突，7 参考规则，8 留出评估，9 已确认排除。10–31 保留。
不能使用 `1 << reason_code`；一个入选代表可以 reason=0 且 bit5=1。
flags=0 只说明此次已执行检查未命中，不代表检查过所有质量维度；coverage 必须区分未检查和通过。
遇到未知字典版本、未知码或未知 bit 的消费者须拒绝解释，而非默认入选。

可选文本覆盖列为selected_text:string与selected_text_source:uint32，均nullable且同时有值或同时null。
另有可选nullable string selected_language：仅在显式alias转换改变值时写入，null回退基础language。
规则支持en-US/en-us→en、zh-CN/zh-cn→zh；不泛化裁掉所有地区/脚本子标签。speaker_id不变。
筛选、去重比较和语言统计均使用同一个选用值；训练读取selected_language优先，不再漏掉地区别名。

只写实际变化的选用文本；null回退基础text。来源码0保留给base_normalization，1及以上固定修订来源。
本次strip后的变化值会实际存入selected_text；空串表示去空白后没有文本，reason必须为非零。
有修订时重复比较、缺文本判断和音文分数都使用最终选用文本/revision；详见05。

## 已确认排除与继承

音频本身问题以完整 audio_sha256 为作用键，包含适用解码器/时间轴/区间时必须一并匹配；
一条文本/配对问题用 (dataset_id,release_id,sample_id,text_revision) 键。
text_revision沿用05的selected-text-v1：对选中的原始或修订文本及完整规范化定义计算摘要；
文本或规范化变更须重新判断证据适用性，不按sample_id盲目继承音文分数。
每项有稳定 exclusion_id、作用键、issue_code、证据、用途范围和带时区记录时间；同对象可有多项。
禁用与原始数据物理删除无关。撤销记录必须指出 exclusion_id、理由和证据，不以“改了阈值”隐式撤销。

首次 selection 明确 parent=null，并列明本次收到并复核的全部历史排除证据来源。
后续生成工具必须显式接收父发布，固定其 manifest/exclusions 哈希；在完整继承基础上记录 additions/retractions。
如刻意重新建独立根，必须写明 reset 的理由和已知排除对账，禁止工具默默默认空排除。
将合并后的有效清单复制到新 selection，校验旧有效项全部保留或有显式撤销。
父引用只用于审计，读取新 selection 不依赖父目录存在；没有 `_shared` 运行时依赖。
复制的是稀疏证据，绝不为数千万条首尾空白逐条记问题。

## 精确重复：先全局计算，再分数据集写结果

同一 selection 中所有输入参与一次完整 SHA256 分组；用编码音频 bytes 的 audio_sha256，
不宣称覆盖不同编码、近重复、部分重叠或同录音切片。短哈希只能筛候选，最终分组必须核对完整 SHA256。
duplicates.lance 保存所有组大小 >1 的成员，含 dataset_id/release_id/sample_id/audio_sha256、
代表 dataset_id/release_id/sample_id（未解决冲突时 null）、组处置及文本/语言差异标志。
唯一键为 (dataset_id,release_id,sample_id)，sample_id 与 audio_sha256 建 BTREE。
它由本 selection 拥有并固定 snapshot；不用给每个正常单例额外保存一条映射。
同一音频组不是同一 speaker 的证据，不因此合并标签。

决策顺序：

1. 标记所有原始成员和差异，传播适用的音频级排除；按样本应用文本/配对排除。
2. 用明确的比较规则检查剩余候选的非空文本与语言。首版比较允许 Unicode strip 首尾空白、
   语言 alias en-US→en / zh-CN→zh；不删标点、不大小写折叠、不改写实词。
   比较规范化不等于改写 base；空文本成员因缺文本排除，不与有效文本制造冲突。
   缺语言是否可候选由用途规则决定，unknown 不是与任何语言相同的证据。
3. 对仍有不同非空文本或不等价语言的组，首版保守排除所有尚未被更高优先级排除的成员，
   reason=4002；其他已排除成员保留自己的主原因，同时保留重复/冲突 flags。
   差异不必然意味着至少一条错误（也可能是合法转写差异）；保守排除表示尚未裁决。
4. 无未解决冲突且存在合格候选时，按 rules 的固定来源优先级，再按 sample_id 字典序选唯一代表；
   不把来源优先级描述为实测质量。其他合格成员 reason=4001。无合格候选则代表=null。
5. 全部数据集使用同一次结果、同一规则文件；新加数据集后生成新的完整 selection，旧发布不变。

同时统计原始冲突组、解决后冲突组、成员数、冗余数、跨来源/跨语言分布；不能混淆组数与条数。
重复表与分支原因/flags/代表须逐项一致。codec 计算可按 feature_key 复用，但去重代表仍由此处的数据规则决定。

## 规范化、用途与评估

原始文本的首尾空白保留；首版训练采用 `unicode-strip-v1` 的选用文本变换，由 rules 固定具体实现，
去重比较也使用它。这是selection中的稀疏派生覆盖值，不往base写新text；不声称上游空白是转换错误。
语言采用明确 alias 表，将 en-US→en、zh-CN→zh；其他标签只按完整列明的映射处理，
禁止通用 split('-')[0] 丢失需要区分的信息。统计原值与选用值的条数/时长，speaker_id 保持原样。
首版训练 selection 发布前必须固定上述规则与支持语言集合；本规范更新不等于生产规则已经执行。

首次正式supervised_tts选择采用用户确认的闭区间1–120秒；区间外为时长策略排除，不等同于损坏。
purpose=supervised_tts 要有音频、有效文本、适用语言与明确时长策略；参考协议 self 不要求 speaker 标签。
purpose=audio_features 可以独立定义纯音频范围，不默认套用文本规则，也不得拿其 reason=0 当可训练结论。
首轮 codec 面向首版训练，复用 supervised_tts selection 的入选范围，避免为已知无文本样本额外算特征。
两种用途的分支不能隐式互换；功能允许不等于这轮要发布两份。

首版 evaluation.status=not_assigned，不声称已经隔离评估集；正式训练必须明确 no_holdout 或生成并验收划分。
需要划分时，以精确重复组、已验证 group_id/recording_id 的连通关系作为不可拆分单位。
未知录音不伪造 group；原神/星铁/AISHELL-3 等缺分组来源只能承诺已覆盖的重复/样本隔离。
seen_speaker 和 unseen_speaker 分开声明；目标与参考都检查隔离。不证明近重复隔离，不把上游 train 当评估方案。

## 写入、发布、恢复与清理

从固定 main version 创建同根分支，按片段流式 add_columns，回调显式读取 sample_id 和所需小列。
全局重复/排除查询应批量有界，不能把所有 ID/文本转 Python dict；不能依赖其他扫描器的隐含相同行顺序。
merge 可用于按键增列，但大表必须经过峰值内存验收；索引不保证 merge 无全扫描。
分支禁止修改基础列、增删行或 compaction。未来确有需要，必须新绑定并重新验收全部定位。

单一协调者串行提交同一表 schema；worker 可并行生成结果，不争写同一分支。
各分支先完成全行原因/flags、统计、唯一性/引用验证，确认基础 main 版本与原文件未变；
然后给基础与输出版本创建保留 tag。所有计划数据集输出齐全、规则/输入一致后才原子发布 selection manifest。
任何分支缺失、pending/null 状态、范围不符、重复裁决冲突均阻止 complete；半成品分支不能消费。
中断后按固定输入与规则核验已提交 snapshot 再复用，不能把已完成数据集偷偷混入新一轮全局计算。

原始 release 的“不可变”指 manifest bytes、基础逻辑快照及 main；允许新增 _refs/tree 和派生数据文件。
审计须按具体 snapshot 的实际引用核验，不能只认 data/ 或把整个 tree/ 当孤儿删除。
新增分支不会使基础 shards 清单变成整个目录文件清单。

所有annotations、selections、features、builds、training_plans的complete manifest及其传递依赖都是保留根。
生产清理前必须枚举依赖图、检查 tag/固定版本、阻止并发写；被引用 branch/version 不能删除或清理。
不要删除保护 tag 绕过引擎检查；目录 rename、外部 shallow clone、手工删 fragment 也不允许当作安全回收手段。
自动回收尚未实现时不运行生产 cleanup_old_versions。实现证据和性能边界记录在 pipeline，规范不宣称已实现发布器。

## 与 codec 和训练的衔接

生成 codec：打开 manifest 固定的 samples 根路径 + branch/version，投影音频相关列，筛 reason=0，
按 06 的 selection_branch 模式记录目标集合，无需再复制全量 targets 或读取一份位置位图。
特征表仍按 target_id/feature_key 关联；分支没有自动保证特征顺序与原始行顺序一致。

准备训练：一次批量匹配特征，在从 selection 固定版本派生的 build 分支添加
nullable int64 codec_row、speaker_row（快照内逻辑行偏移），以及 build_ready 布尔状态。
绑定 manifest 固定所指特征表、profile/run/main version，逐条核对 target、音频区间、指纹、status=ok。
禁止靠写入顺序推断对应；nullable 只表示该特征未就绪，不用 -1 或 0 冒充缺失。
两列全库原始数值约 2.16 GB，另加 null bitmap/元数据；按需要生成，不在当前 selection 强制预建。

build 分支不覆盖 selection_reason；特征失败与数据被排除是不同事情。
训练使用 reason=0 且 build_ready，按批量 take 读取固定特征快照，断言 locator 对应的 target_id。
换 feature run/snapshot 必须重新生成绑定；改采样权重无需改分支或复制 codes。
indexed_references 仍需训练端真实吞吐验收，见 07；当前只确定接口，不表示训练读取器已实现。
