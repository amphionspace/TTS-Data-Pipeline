# 05 · Annotation 结果表、输入依赖与结果状态

## 各层职责

```text
固定版本的 samples：原始音频、文本和来源
    ↓
独立 annotations：检测证据、候选文本修订、恢复音频引用
    ↓
selection：固定采用的音频对象、文本版本、入选状态及原因
    ↓
独立 codec / speaker embedding / text
    ↓
新的 merged 版本
```

- samples 保留来源事实，annotation 不覆盖原文、音频或来源语言。
- annotation 保存测量、候选修订及版本化质量判断；一次模型输出不自动成为正确答案。
- selection 采用固定 annotation 版本，决定训练资格和最终内容；排除不通过改写 annotation 的状态或结果表达。
- 语言、说话人、句长等覆盖属性单独保存。训练比例、权重、预算和 batch 由训练端管理。
- 原始版本和三个独立特征表继续保留；merged 是额外交付，不解除上游审计依赖。

## 存储边界

一对一样本标注默认使用独立单表（storage_kind=sample_table），一对多使用 result_table 双表，
都不向 samples main 增列；结果表归所属 dataset/release，布局见下节。基础 27 列、main 和基础 manifest 保持不变。

| 结果类型 | 存储 |
| --- | --- |
| 每样本一条、独立保存状态与可空结果 | 单张 results.lance，storage_kind=sample_table（默认） |
| 每样本多个事件、字词对齐、多个候选 | 独立 results.lance + targets.lance，storage_kind=result_table |
| 每样本有界的区间列表，总与样本摘要一起读取、不按条目编号引用（如说话人结构） | sample_table，result 内含列表；manifest 记 layout_reason |
| 每样本一个结果，需与 selection 按位置组合 | 可选：annotation 分支的任务 struct 列，storage_kind=sample_branch |
| 文本纠错、重新转写 | 文本修订 struct，明确 keep/replace 等状态，见[文本修订](#文本修订与selection采用) |
| 改变实际音频区间、切分 | 由 selection 产出实际片段；不能只改 annotation 来冒充新的音频输入 |

sample_table 的状态和结果保存在同一行，不另外创建 targets 表。
已有 result_table 的一对一任务保留兼容读取，迁移必须明确 schema/layout 版本；manifest 固定布局和原因。
旧 storage_kind=sample_column 与 ann__task__run 列名只作兼容读取，新任务不继续向 main 追加这些列。

选用 sample_branch 时，annotation 分支与 selection 都直接从固定基础 main 版本 bv 建立；
不把 selection 建在 annotation 上，也不通过继承上一份 annotation 来组合任务：

```text
samples.lance（main，固定 bv）
├── ann/<task>/<run_id>        # 只增加本任务结果列
├── ann/<other_task>/<run_id>  # 同样从 bv 建立
└── <selection_id>            # 从 bv 建立，组合固定的 annotation 结果
```

分支名由引擎管理，不手工创建 tree 目录。列名使用固定任务名或指标名，不包含 run 编号。
不强制为少量结果创建全行结果列；稀疏空列有 bitmap/元数据开销，不承诺零存储。
分支隔离不能代替任务输入校验；sample_id 仍是业务身份，位置只是可验证的读取优化。

task 使用小写字母/数字/下划线，不含路径分隔符。
run_id 使用 `tts-ann-<task>-YYYYMMDDTHHMMSSbjt-NN`，首次创建的北京时间、至少两位正整数序号；
重试沿用原名，原子占用避免重名。

## Run 发布与多数据集

单数据集和跨数据集任务均由一份 `annotations/<task>/<run_id>/manifest.json` 发布。
分支实际位于各 dataset/release 的 samples.lance 内；大结果表仍归所属 release：
`datasets/<dataset_id>/<release_id>/annotations/<task>/<run_id>/{results.lance,targets.lance}`。
不另造一份带基础音频/文本的全局表，不要求每个 dataset 再写重复的子 manifest。
同一跨数据集 run 在各表使用相同分支名，manifest 的 outputs 按 dataset/release 唯一列出固定版本。
所有计划输出齐全、校验和保护 tag 完成后，才原子发布全局 manifest；中途失败不发布部分 complete。
每个任务执行前固定 dataset/release、基础 manifest 摘要、表快照及目标集合；尚未发布或仍在变化的数据源不在运行途中加入。
抽样可以使用固定 ID 子集；全量质量档案覆盖固定 base 的目标范围，不能默认只检查旧 merged 的入选样本。
各任务独立写入与发布；worker 通过分片及提交协调器汇总，不要求多个模型并发修改同一张表。

manifest 固定 contract/schema、task/run/profile（含完整定义与摘要）、实现/依赖、北京时间 finished_at，
以及 inputs、outputs、检查覆盖。所有外部路径相对统一根，禁止 latest。
每个 input 固定 alias、manifest 路径/原文件SHA256、table/branch/version、dataset/release 和输入角色。
每个 sample_branch output 固定 base_input_alias、table_path、branch、lance_version、tag、table_rows、
columns（每列的profile/schema/选择范围/覆盖数）、基础不变/位置一致/输入指纹验证统计。
result_table output 则列出 tables.results/tables.targets 的路径、版本、schema 与行数。
sample_table output 使用单个 table 引用，包含相对统一根的 table_path、branch、lance_version、tag、rows、schema/schema_sha256；
coverage 按本次目标范围统计，行数包含成功和失败。

columns 每列明确保存 profile/profile_id 与 schema/schema_sha256；schema 为包含该 struct 列的 Arrow 类型描述。
即使只含一个指标，也明确填写，不靠隐式继承顶层配置。
columns 各自统计，不把两个指标的结果数相加当成样本条数。跨数据集汇总同名列时才对相应coverage求和。
任意子集在执行前固定目标ID集合/选择表及其hash；all_base_samples 明确指固定bv的全体。
运行过程中外层null可表示未完成；complete时范围内missing=0，范围外仍为null。
complete表示所选目标全部终态记账，不等于全部成功，也不等于整张base全覆盖。
示例见 [annotation manifest](../examples/annotation-manifest.example.json) 和 [质量结果](../examples/quality.example.json)。

## Struct 与缺失语义

一对一列的类型沿用 `{status, input_fingerprint, error_code, result}`：

- 整个struct为null：该run在该位置没有终态结果，需结合固定选择范围区分未选与未完成。
- status=ok：输入指纹非空，任务结果有效，error_code=null；真正的0分保持数值0。
- status=failed/unsupported/skipped：输入指纹与error_code非空，result=null。
- 不用NaN、Infinity、-1或0代替缺失/失败。任务若合法允许负数，不能把负数当统一缺失码。

每个任务固定指标定义/单位/方向/合法范围、适用来源/语言、模型版本和全部实际输入依赖。
missing、failed、unsupported、skipped 如何影响入选，由selection逐任务显式决定；不能自动降为低分。
上游没有分数的导入目标为skipped/upstream_missing；格式错误为failed/upstream_invalid。
这两种均表明导入器已检查，不用外层null冒充“未处理”。
依赖的上游任务失败时可记 skipped/upstream_failed，与之无关的任务继续；某个质量分数低不会自动终止其他计划标注。
模型加载、身份或来源冲突等普遍性错误停止相应阶段，不能作为大量坏样本静默跳过。
结果通过 manifest/profile 取得公共元信息，不要求每行重复模型配置和长路径。

一对多结果用targets记录各目标的status/input_fingerprint/error_code/item_count，
results仅存成功事件，(target_kind,target_id,item_id)唯一。ok且item_count=0表示检查过但无事件。
目标表固定范围并完整记账；结果表的 sample 目标必须存在，所有成功事件数与item_count对账。
整个输出没有事件时，仍发布带固定 schema 的零行 results 表及完整 targets 表，不能省略终态记账。
按target_id建BTREE辅助查询，索引不提供唯一性或外键保证；一对多不使用基础行位置直接对齐。

## 独立一对一样本表

storage_kind=sample_table 使用单张 results.lance，每个范围内 sample 恰好一行：
`sample_id, input_fingerprint, status, error_code, result`。
前三列不可为空；result 是任务固定的 nullable struct。ok 时 error_code=null、result 非空；
failed/unsupported/skipped 时 error_code 非空、result=null。整行缺失表示未完成或不在范围，不能冒充失败。
完成时覆盖目标必须全部终态记账，missing=0；全部失败时仍有完整行数，不能发布零行表。
manifest 固定 base 快照，sample_id 关联样本身份；不要求与基础表行号相同。sample_id 建 BTREE。
一对一表不需要 target_kind/target_id/item_id/item_count，因为类型是 sample、每目标固定一行。
任务内部可保留兼容的双 Parquet 检查点作为运行实现细节，不能对外解释为两张正式 annotation 表。

## 时间和文本坐标

音频证据统一映射到对应音频对象实际解码后的原生采样点坐标 `[start_sample, end_sample)`，
满足 `0 <= start_sample < end_sample <= decoded_num_samples`，同时固定采样率和解码时间轴。
模型分帧步长、偏移、重采样映射及边界取整写入 profile，保留真实定位精度；整数采样点不代表采样点级准确。
annotation 的音频区间不是 codec 帧号，不能因某个 speaker reference 使用 codec 边界就共用坐标。

文本差异使用明确字符串上的 Unicode code point 半开区间；词级结果另记录分词单元及其到文本的映射。
规范化前后分别标明坐标所属文本。不具备可靠音频定位的文本差异，其音频范围为空。
整句评分不伪装成字词评分，窗口评分不解释为窗口内每个时刻均有缺陷。

## 物理平级与逻辑依赖

物理父分支始终是同一个bv；计算依赖可以形成无环图：例如base → ASR修订 → 音文一致性 → selection。
后一个annotation只在inputs记录前一个固定run/branch/version/column，不继承其分支。
拒绝循环依赖、缺失引用、不同bv的隐式按位置组合；新任务不得引用自己将要产生的selection。
多个annotation的组合通过显式读取完成，不要求Git式branch merge，也不能把Lance按键merge误称为分支自动合并。

input_fingerprint由任务profile声明的输入对象规范JSON计算SHA256，至少覆盖目标身份和实际依赖：
纯音频指标绑定audio_sha256、时间轴/区间和前处理；音文一致性另绑定实际text_revision；
ASR/文本修订绑定实际输入音频和被修订文本（若使用）。来自相同sample_id不代表不同文本的分数可互用。
只换无关文本不使纯音频codec/embedding失效；改音频区间、权重、前处理或profile则需重新验证/生成。

## 位置对齐协议与写入验证

只加列是必要条件，不是充分证明。不得修改基础列、追加/删除/重排行、overwrite或compaction。
发布后结果列也不可覆盖；补跑新run。增加索引可以提交新快照，但发布固定确切版本。
生产写入器须逐目标核对身份/指纹，不把乱序GPU结果直接交给按行增列reader。

位置优化仅用于同一base路径/manifest/bv派生的sample_branch和selection：

1. 验证全行数、基础列/文件引用未变，以及有序sample_id流与bv逐行一致；仅行数相同不够。
2. 用同一逻辑行区间批量take，或各分支显式有序无过滤扫描后重新对齐批次边界；对齐后再做筛选。
3. 校验同位置sample_id相等，遇到差异立即停止。不能直接zip两个to_batches迭代器。
4. 禁止独立过滤/排序后按位置拼接；fragment扫描范围和顺序也须一致。
5. take的逻辑行偏移与_rowid/_rowaddr不同；不能互换，也不能把其他snapshot的位置直接沿用。

这是避免ID hash join的受控路径，不是零扫描、零I/O；多分支仍需投影读取和身份校验。
如不满足这些约束，只能显式按业务键重新绑定并验证，不能悄悄退化为猜测行序。
发布验证结果保留row_count、ordered_sample_ids_sha256、基础引用/字段核查覆盖和检查器版本。
有序ID摘要对无过滤顺序中的每个sample_id UTF-8加换行流式SHA256，不能以排序后摘要代替。
sample_branch 的生产执行器与自动位置对齐验证器尚待实现；独立 ASR 执行器不表示任意 annotation 任务已就绪。

## 任务与结果解释

task 名称及其逻辑结果如下。每个任务在生产运行前须冻结 profile：模型 ID、权重 revision/摘要、推理实现、
数值精度、前处理、分块与拼接、语言路由和指标定义；不引用 latest 或隐式默认值。
模型只在明确支持并经验证的语言上启用；上游转写模型已知时记录来源，同源重复识别不称为独立核验。
已有完整定义的任务见本章对应小节，其余任务名只登记用途，实现前补齐字段与 profile。

| task | 逻辑结果 |
| --- | --- |
| `audio_stats` | 整段解码的确定性波形统计，见[独立音频统计结果](#独立音频统计结果) |
| `spoken_language` | 音频语言、模型置信输出、分析范围、与来源语言的冲突，见[FireRedLID 音频语言单表](#fireredlid-音频语言单表) |
| `transcription` | 独立 ASR 文本、识别或指定语言、实际处理范围，见[独立 ASR 转写结果](#独立-asr-转写结果) |
| `upstream_quality` | 上游提供的质量分数导入，见[上游质量导入与采样](#上游质量导入与采样) |
| `audio_quality` | DNSMOS P.835 的 SIG/BAK/OVRL、校准前后值、窗口范围及窗口均值/最低值/数量 |
| `text_agreement` | 原始/规范化 CER、WER、分母、增删改数量、文本差异范围 |
| `text_alignment` | 字词单元、文本范围、音频范围、时间异常 |
| `speaker_structure` | 样本内 speaker 区间、估计人数、重叠区间及摘要，见[MOSS 说话人结构单表](#moss-说话人结构单表) |
| `text_revision` | `action=keep/replace`、候选文本、修订依据，见[文本修订与selection采用](#文本修订与selection采用) |
| `restoration` | 执行状态、派生音频引用、输入/输出哈希、采样率/长度及处理谱系，见[音频恢复](#音频恢复) |
| `quality_summary` | 四维判断、原因、证据引用、覆盖属性，见[质量汇总](#质量汇总) |

结果解释：

- DNSMOS 是预测听感，不是音文正确率。短音频的官方流程会重复自身，若采用必须记录输入延拓方式；
  重复后的窗口不能冒充真实的多个音频区间。不同 MOS 模型或来源的分数先做对照，不直接混合。
- 低能量长度仅表示固定阈值下的波形统计，不能解释为无语音区间。音文长度比使用当前选用音频的完整实际时长，
  不扣除低能量区间或首尾停顿，不解释为有效语音时长或真实语速；是否用于过滤由 selection 规则决定。
- ASR 不使用被核验文本作为提示。来源语言和实际指定给模型的语言分别记录；语言冲突保留证据。
- CER/WER 以被核验文本为 reference，ASR 为 hypothesis；保存替换/删除/插入绝对数量，允许比率大于 1。
  reference 为空时比率为空并注明原因；不适用的 WER 不用 0 占位。比较规范化不直接改写训练文本。
- 对齐保存时间戳，不构造模型未提供的声学支持分数；对齐成功不能证明原文正确。
  对齐器不支持的语言明确记为 unsupported，不因此直接判样本不合格。
- speaker 标签仅在当前音频对象内部有效，不写回全局 speaker_id；保留重叠，不强制只允许一个 speaker。
  重叠比例为重叠区间并集时长除以实际分析音频范围的完整时长；speaker 人数是估计值。
  未执行或失败时结果为空，不能把没有结果当作零重叠。
- 首尾低能量和对齐异常属于完整性证据，不能单独证明音素截断或异常拼接。
- 模型置信度不命名为经校准的正确概率。

## 独立 ASR 转写结果

`qwen-asr-transcription-v2` 使用 sample_table，独立保存全量转写证据、原始响应及语言冲突候选，
不修改基础表。单张 results.lance 每个 sample 一行：sample_id、input_fingerprint、status、error_code、result。
成功行 result 非空，失败行 result=null；成功但 text 为空字符串与失败必须区分。
主转写及指定语言候选是同一个 result 内的字段，不为候选增加样本行。
历史 `qwen-asr-transcription-v1` 为 targets/results 双表（item_id 固定为 0）；唯一发布的 v1 run 已迁移为 v2 后删除，读取器只需支持 v2。
已发布 v1 迁移到 v2 时发布新的 run，不覆盖原版；layout_migration 固定来源 annotation manifest 路径、SHA256、
来源 run/schema 与目标 schema。仅存储合并时复用原 profile、profile_id、输入指纹和 ASR execution，
publication_code_sha256 单独记录迁移代码；全字段回读、ID/指纹/状态核验后再发布 complete manifest。
每个输出的 manifest 固定任务 Arrow schema 与摘要，不把 ASR 输出自动登记为选用文本。

result 记录 audio_sha256、原生采样率及完整实际输入的 start_frame/end_frame、
source_language/source_text_sha256、text/language、language_mismatch、请求音频帧数和摘要、
raw_response_json，以及可空的 language_candidate_text/language_candidate_response_json。
主转写不使用原文提示；语言候选与独立识别分开，不能把被指定的语言当独立检测结果。
原文从固定 base 输入读取，source_text_sha256 是 canonical JSON 原文本值（包含 null）的 SHA256。

输入指纹为 `SHA256(canonical_json(["transcription-input-v1", sample_id, audio_sha256,
"full_actual_decode", source_language, profile_id]))`，来源语言用于冲突候选。
模型版本、音频处理、请求协议、生成参数和服务端长音频处理固定在 profile。
主音频一次完整提交；服务内部未提供时间戳时，不推测分块的字词或音频坐标。
协议标记不能混入最终 text；空转写不等于执行失败，也不等于训练合格。
文本和音频均不回退或覆盖基础值。是否采用转写由后续新 selection 明确决定。

### 上游 transcription 导入

Emilia Refined Metadata 导入继续使用 `qwen-asr-transcription-v2` / `sample_table`，
Arrow 字段、类型及可空约束与本地 ASR 一致；以新的 profile/run 区分执行方式。
`qwen3-asr-1.7b-emilia-metadata-v1` 的 `execution_kind=upstream_metadata_import`，
模型名称由上游说明提供，未知权重版本、推理参数不继承本地服务配置。
JSON `text` 原样写入 `result.text`，`language` 作为本批 ASR 语言规范成
`de/en/fr/ja/ko/zh` 写入 `result.language`；`source_language` 保留固定 samples 的来源语言。
语言字段来自上游 metadata，不额外声称其生成方式为自动检测；冲突仍只是比较证据。

`audio_sha256`、`native_sample_rate`、`source_text_sha256` 来自匹配的基础样本；
它们绑定样本，不证明上游推理波形及前处理与本地相同。
没有提供的 `raw_response_json`、`request_audio_sha256`、`request_frames`、
`start_frame`、`end_frame`、`language_candidate_text`、`language_candidate_response_json`
均为 null。忽略上游 timestamps，不把 metadata duration 换算成推理范围。
消费者按 profile 解释空值，不能要求每种 transcription 执行方式都有本地请求证据。

导入指纹为 `SHA256(canonical_json(["transcription-metadata-input-v1", sample_id,
audio_sha256, source_text_sha256, source_language, metadata_relative_path,
metadata_file_sha256, upstream_id, profile_id]))`。
manifest 固定 base 快照、metadata 文件清单与摘要，以及导入实现。
以 metadata 文件对应的原始 tar/source_snapshot 和 upstream_id 关联 samples；
不能从 ID 中的批次号推断 tar 文件。全量逐文件覆盖、固定 base 的有序 ID 和全字段回读通过后发布。
身份冲突或缺失阻止发布；非法文本类型或语言值可记为 failed，不能回退原文。
成功返回的空文本、纯标点文本保留，质量筛选留给 selection。

全量 ASR 可覆盖任意本次明确纳入的已发布 base，范围由固定 inputs 决定，不限于游戏。
`qwen3-asr-1.7b-base-v2` profile 固定来源语言到 Qwen 语言名的路由；主转写仍自动识别，
来源语言仅决定是否另存指定语言候选。旧游戏 run 保留原 profile，不因扩展语言而重跑。
大表不重复执行全列 sample_id 去重：基础唯一性继承固定、已通过唯一性验收的 base manifest；
计划和发布分批读取有序 ID，核对空值、总行数及有序摘要，每个任务再核对准确 ID 范围。
发布全字段回读和有序 ID 一致性共同验证样本对应，不使用超过 Arrow 字符串容量的整列聚合。

执行层可对临时连接/读取中断及 HTTP 408/429/500/502/503/504 做有限请求重试，耗尽后等待并自动恢复固定检查点；
尝试次数、超时及恢复间隔记入 execution。失败证据必须在发生时自动记录，不以人工检查作为记账触发。
私有 failures.jsonl 保存目标、指纹、时间、主转写/语言候选/基础设施阶段、原因及可得错误详情；
可能包含重试事件，不能代替正式单表（历史版 targets）的唯一终态统计。
已验证的少于 160 个 16kHz 采样点的全零请求被 Qwen3ASRProcessor 以 HTTP 400 拒绝时，
记为 `failed/asr_audio_preprocessing_rejected`；不补零、不回退原字幕。未知请求错误和模型/来源冲突不得静默略过。
输出达到生成上限但未正常结束记为 `failed/asr_output_truncated`，不能自动作为有效文本发布。
正常结束但主转写正文缺少可解析的 ASR 格式标记时，同一请求最多尝试 3 次；耗尽后记为
`failed/asr_output_invalid_format`，原始响应及重试证据进入失败日志，不把无语言标记的正文猜作有效转写。
若后续尝试成功，之前的异常响应保存在 `raw_response_json.invalid_format_attempts` 中。
指定语言候选失败仅记录候选证据，保留成功的主转写。
同批至少 8 条且至少 25% 的样本在重试后仍发生主/候选格式异常，停止且不提交该批，
防止服务协议故障被静默登记成大量坏样本。未知结束标记、传输协议错误、模型/输入变化仍须停止排查。
同一未发布 run 的运维修复必须记录精确实现迁移及兼容验证，保持原计划/输入指纹/已验收检查点，
并在最终 manifest 中记录实际执行代码与迁移；改变模型、音频处理或生成语义需要新的 profile/run。


## 文本修订与selection采用

文本修订result固定 `{action, text}`：ok/keep时text=null，ok/replace时text必须为有效非空文本。
外层null为未处理；failed不等于keep。首版不支持用null表示clear；确认不可用文本通过明确排除表达。
以后扩展clear需新任务schema和显式selection读取语义，不能与“回退base”混用。

selection按rules固定的run优先级与缺值策略选择文本，不用更新时间/latest决定。
采用修订时，在selection增nullable string selected_text，以及nullable uint32 selected_text_source：
覆盖值与来源码同时存在；其余两列同时为null，回退基础text。来源码指向manifest的text_sources字典。
0保留给base_normalization：对基础文本去掉首尾空白后实际变化的值。1及以上表示固定文本修订来源。
annotation替换必须非空；base_normalization得到空串时保留该覆盖值，但该行必须按缺文本排除。
source记录准确annotation manifest/分支/列和输入bv；不单独凭uint8码假设可支持任意多来源。
基础文本本来为空且无有效替换时仍是缺文本；不以空串或null修订伪造成功。
通用strip按rules在采用文本后应用。本次selection将发生变化的文本写成稀疏selected_text覆盖，
未变化的沿用base；不把内部空白、标点或内容一起清洗。selected_text已是最终规范化值，读取不能按truthiness回退：
必须判断is not None，以免把已清为空串的文本又替换回原值。其他规范化须明确输入/输出顺序。

text_revision统一使用
`SHA256(canonical_json(["selected-text-v1", selected_raw_text, normalization_definition]))`，
selected_raw_text为选中的原始或修订字符串（也可null），normalization_definition包含完整算法/版本/参数。
来源谱系独立记录；相同文本及规范化可以保持相同revision，不因只换run名使音文结果无意义失效。
旧任务声明其他fingerprint/text_revision算法时不能混用，须显式重新核验或计算。
音文指标必须对应这一实际revision；换文本后旧分数不自动沿用。
当文本来自修订且随后被规范化时，不能用最终 selected_text 冒充 selected_raw_text。
发布 selection 或物化 codec 文本时，需从固定 annotation 输入读取规范化前的修订原文，
按同一 normalization_definition 计算并核验 revision；不要求训练每步回读 annotation。
仅实现基础文本来源的执行器，不得宣称支持任意 annotation 文本修订。
重复组比较采用最终选用文本/语言，修订可能改变冲突和代表，必须重新全局裁决再发布selection。
codec 发布物化时读 selection 的覆盖值；消费者直接读固定文本特征快照的选用文本，不逐 step 查询 annotation；这些小列的空间需实测，不假设null完全免费。

## 音频恢复

restoration 在档案中表现为一个结果项，但恢复音频按 [派生音频约定](02-base-schema.md#音频与派生)
独立保存，具有独立派生身份；不能只用父 sample_id 区分两种波形，也不能把波形塞进普通质量分数或覆盖原 sample.audio。
结果引用固定派生对象身份、产物 manifest/table/version、父音频、变换 profile 和输入输出摘要。
派生音频的物理布局与 selection 消费 schema 在实现时冻结并补入本章。

恢复前后分别绑定音频对象，保存或引用音质、ASR/音文一致性、时间关系和必要的音色保持核验。
MOS 上升不自动证明恢复成功；不能把一次转写变化直接解释为原文或恢复音频错误。
模型输入重采样、边界处理、输出实际长度和编码方式明确记录，不默认原生采样点一一对应。
已存在上游恢复处理的来源记录处理谱系，不仅凭 dataset 名称决定重复恢复或跳过。
普通 restoration 按单说话人处理；多人分离不是其隐式功能。
失败保留状态，selection 可以明确采用已核验的原音频，但不能把回退伪装成 restoration 成功。

采用恢复音频后，针对该对象重新生成 codec、speaker embedding、alignment 和相关质量结果。
随机 reference 的区间及其到完整 codec 的映射也必须重新建立与验证，不沿用旧波形的定位。
文本内容可经核验保留，但文本特征和 merged 必须绑定新的正确音频身份；旧产物继续保留。

## 上游质量导入与采样

Emilia/YODAS：`json.loads(metadata_json)["upstream"].get("dnsmos")` → upstream_dnsmos列。
Wenet：`json.loads(metadata_json).get("upstream_dnsmos_p808")` → upstream_dnsmos_p808列。
可发布upstream_quality任务，两个指标分别使用struct列/指标profile；明确各列的适用dataset和选择范围。
只接受非bool的有限数值，固定源字段路径、来源快照与解析规则；未知模型版本记unknown。
这属于导入，不声称本地重算；未经校准不混用阈值。初版selection不依赖质量时无需先导入。
按投影批次读取metadata，不加载audio；空间统计包含struct/指纹/索引，不能只按一个float估算。

### Emilia 系列上游 DNSMOS（upstream-dnsmos-p835-ovrl-v1）

`upstream_quality` 任务的 `upstream-dnsmos-p835-ovrl-v1` profile 导入 emilia、emilia_yodas、emilia2 上游自带的
DNSMOS，使用 `sample_table`：每个固定 base 样本恰好一行，顶层 `sample_id, input_fingerprint, status, error_code, result`。
范围为三者已发布固定版本的全部样本（all_base_samples）。同一任务以后可以用其他 profile 导入别的上游分数
（如 Wenet 的 P.808），各自独立 run，不与本 profile 混用。

| 数据集 | 源字段（metadata_json） | 粒度 |
| --- | --- | --- |
| emilia、emilia_yodas | `upstream.dnsmos` | 每条 utterance |
| emilia2 | `upstream.short.dnsmos` | 每个 short 片段（每个 base 样本即一个片段） |

| result 字段 | 含义 |
| --- | --- |
| audio_sha256 | 该样本 base 编码音频身份 |
| upstream_dnsmos | 上游分数，按解析结果原样保存为 float64，不四舍五入、不重算、不截断 |

profile 固定：
- 指标：DNSMOS P.835（`sig_bak_ovr.onnx`，ONNX Runtime）多项式校准后的 OVRL，非 personalized，只有 OVRL（没有 SIG/BAK）；
  越高越好，预期范围 [1, 5]；
- 三个数据集使用同一模型（Emilia-Pipe 与 Emilia2-pipe 均为 microsoft/DNS-Challenge 的 `DNSMOS/sig_bak_ovr.onnx`），
  由数据负责人提供（model_attribution=user_provided）；上游实际模型文件的 SHA256 未提供时记为 null，不推断；
- 输入延拓：官方 DNSMOS 对短于 9 秒的音频重复拼接，属于上游处理，无法逐条核验；
- 各数据集的上游过滤阈值只作说明，记录在 profile 中，不是本导入或 selection 的规则：
  Emilia 3.0，Emilia-YODAS 2.4，Emilia2 英文、中文 3.0，其余语言 2.4；依据为数据负责人说明与数据中的硬截断。

解析规则：用 orjson 解析整段 `metadata_json`（不接受 NaN/Infinity 字面量；记录 orjson 版本），再按上表取字段。
- 字段不存在或为 null：`skipped/upstream_missing`；
- `metadata_json` 无法解析、字段路径的中间层不是对象，或值不是有限数值（bool 先于数字判断）：`failed/upstream_invalid`；
- 其余为 ok。超出 [1, 5] 的有限值仍为 ok，由 manifest 按数据集统计 `out_of_expected_range`，是否使用由 selection 决定。

input_fingerprint 为规范 JSON 的 SHA256：`upstream-quality-input-v1`、sample_id、audio_sha256、dataset、源字段路径、
`metadata_json` 原文 UTF-8 的 SHA256、profile_id。只读取投影列，不读取音频。
固定计划与实现摘要后按 fragment 写检查点，恢复时核验；发布前要求每个数据集的状态计数之和等于 base 行数，
全部输出完成回读、`sample_id` BTREE 索引和版本 tag 后，最后发布全局 manifest。

annotation保留测量事实；selection固定资格/原因和采用结果；训练端管理质量到权重的映射、语言比例与预算。
可按需要在 selection 或下游消费表中投影少量选用分数并记录来源，避免训练反复解析JSON；不将某次实验权重变成selection默认列。

## 质量汇总

`quality_summary` 为每个目标物化一项轻量档案，引用详细证据，不复制音频或所有逐帧输出。
按声学质量、音文配对、语音纯度、片段完整性分别输出：

```text
verdict: pass / fail / uncertain
reason_codes
evidence_refs
assessment_profile_id
```

只有已执行且适用的证据可以支撑判断。证据缺失或冲突保留 uncertain，
即使汇总程序成功执行，也不能把所有维度默认置为 pass。
不生成综合 quality_score，不把多个分数相乘，不在汇总中写死 MOS/CER 的入选阈值。

覆盖属性保存来源、语言、speaker 来源、句长、实际完整音频时长、音文长度比、数字/混语等可验证属性及已有重复关系引用；
不为齐全而默认新增专名识别或全局声纹聚类。口音、低沉嗓音、情绪、长句、快速语音不天然扣分。
去重最终裁决和资格规则归 selection；语言比例与采样权重归训练端。

## 全量前验证

新任务或新 profile 先固定跨数据集、语言、时长和已知问题的试验集，人工核验量按问题覆盖和误判情况调整。
评估使用人工核对的文本或判断，不能直接把当前 base 文本当作绝对真值。
除识别 CER/WER 外，重点检查漏词/多词、专名、误删正确样本、缺陷漏检和来源偏差。
不同模型的相同错误不能作为独立确认，人工验证保留按原录音来源隔离的样本。全量前必须验证：

1. 输入身份、文本版本和各时间轴可追溯，恢复音频与原音频不会错配。
2. 重跑、失败重试和不同 worker 数不改变目标身份；有随机性的处理固定种子及策略。
3. 满载分桶/批处理与单条基线对照，模型原生边界处理与 mask 经验证，不引入跨样本污染。
4. 统计按语言和来源的成功率、unsupported/failed/skipped、质量分布及人工误判。
5. 测量包含 Lance 读取、解码、推理和写回的吞吐、存储、显存及预计总耗时。
6. 发布前全部计划目标终态记账、结果唯一性和引用校验通过，固定快照再发布 complete manifest。

试验产物保存在 pipeline 的 artifacts，显式标明用途，生产 selection 不自动消费；
正式运行使用上述发布布局且显式绑定通过验证的 profile。

## 生命周期与保留

全部annotation manifest及其传递依赖纳入保留根。selection引用的annotation版本/表/保护tag不能删除或清理；
独立重跑不表示可以删除仍被引用的旧run。复制了selected_text也不自动解除审计依赖。
基础增加数据需新release/明确版本，旧分支继续服务旧bv；新bv重新建立分支和定位关系。
音频/文本输入指纹未变的目标可经核验复用旧结果，不要求重跑全部模型推理。
改变bv不能直接沿用旧行位置；保护/回收要求见 [09](09-lance-operations.md)。

## 独立音频统计结果

`audio-stats-v1` 的 `audio_stats` 使用 sample_table，单张 results.lance 保存所有目标的状态与可空 result，
不改变 samples main。成功和失败各占一行；顶层字段为 sample_id/input_fingerprint/status/error_code/result。
运行固定所有纳入的已发布 base 快照；未发布数据不在过程中加入。

profile `mono-mix-waveform-stats-v1` 统计模型实际读取的波形：

| 项目 | 定义 |
| --- | --- |
| 解码 | `soundfile.read(dtype="float32", always_2d=True)` 一次读取整段，不分块、不 seek |
| 声道 | float32 声道算术平均 `audio.mean(axis=1, dtype=np.float32)`，与 codec 前处理一致 |
| 累计 | 在 float32 单声道波形上以 float64 累计 |
| 处理 | 原生采样率；不重采样、不归一化、不裁剪、不补零、不限幅；1.0 为满幅基准 |
| 窗口 | 从 0 开始，长度 `max(1, native_sample_rate // 50)`，互不重叠；末窗使用实际长度 |
| 低能量 | 单声道窗口 RMS ≤ `10**(-50/20)`；相邻窗口合并，坐标为原生采样点半开区间 |

整段读取是必需条件。分块/定位读取在部分 MP3 上产生块边界局部差异，
并会把 Ogg/Opus 的读取长度凑成头信息长度而掩盖实际差异；因此不能以分块结果冒充本 profile。
profile 固定 libsndfile、NumPy、soundfile、Arrow、Lance 和代码摘要；模型依赖为空。

`status=ok` 时 result 包含以下 19 个字段，全部是真实测量值：

| 字段 | 定义 |
| --- | --- |
| audio_sha256 | 实际编码音频身份；sample_id 和 input_fingerprint 位于顶层 |
| native_sample_rate / source_channels | 实际解码采样率与输入声道数；分析声道数固定为 1，写入 profile |
| decoded_num_samples / duration_seconds | 整段实际每声道采样点数 N 及 N / 采样率 |
| num_samples_delta | N 减固定 base 的 `num_frames`；非零表示头信息长度与实际解码不同 |
| peak / rms / dc_offset | 混声波形的最大绝对值、`sqrt(mean(x²))`、`mean(x)` |
| near_full_scale_ratio | `abs(x) >= 0.999` 的采样点数 / N |
| low_energy_ratio | 低能量区间覆盖的实际采样点数 / N，按采样点加权 |
| leading / trailing_low_energy_samples | 接触起点、终点的低能量区间长度；没有为 0 |
| longest_low_energy_samples | 全部低能量区间的最大长度；没有为 0 |
| longest_internal_low_energy_samples | 满足 `0 < start < end < N` 的低能量区间最大长度；没有为 0 |
| window_rms_p10 / window_rms_p90 | 全部窗口线性 RMS 的 10/90 百分位，`numpy.percentile(..., method="lower")` |
| longest_internal_zero_run_samples | 混声波形中不接触任一端点的连续精确 0.0 最大长度；没有为 0 |
| decoder_warnings | 解码期间 libsndfile/mpg123 写入 stderr 的行数 |

整段低能量时首、尾、最长长度均为 N，内部最长为 0。dB 值、去直流 RMS、峰均比和头信息比较按需推导，不重复物化。
这些是测量证据：近满幅不等于已确认削波，低能量不等于无语音，内部零值不等于已确认掉音，
decoder_warnings 非零不等于源文件已确认损坏，正常统计也不等于训练合格。

失败时 result=null，error_code 为：

| error_code | 条件 |
| --- | --- |
| audio_decode_failed | libsndfile 无法解码 |
| audio_empty | 解码得到 0 个采样点 |
| native_format_mismatch | 实际采样率或声道数与固定 base 不同 |
| audio_nonfinite | 解码波形含 NaN/Inf，在混声前检查 |

音频 bytes 的 SHA256 与 base 不符时停止运行，不记作单条失败。整批任务全部失败等系统性错误也应停止排查。
头信息长度差异只记录在 num_samples_delta，不补齐、不截断，也不单独构成失败。

输入指纹为 `SHA256(canonical_json(["audio-stats-input-v1", sample_id, audio_sha256,
"full_actual_native_decode", profile_id]))`，不绑定无关文本。
私有检查点是与发布 schema 相同的单个 results.parquet，逐条核对固定 base 范围的 ID 顺序；
全量覆盖继承经过唯一性验收的固定 base 身份，通过固定 fragment/range 的无重复计划覆盖所有行。
发布时每个 release 写一张 results.lance，完整回读所有字段，建立 sample_id 索引并打保护 tag，
最后发布全局 `annotations/audio_stats/<run_id>/manifest.json`。私有检查点不是生产读取入口。

是否及如何使用这些统计由具体 selection 规则固定；未经校准的字段只作诊断，不设硬阈值。

## FireRedLID 音频语言单表

`firered-lid-v1` 的 `spoken_language` 使用 `sample_table`。每个固定 base 样本恰好一行，
顶层为 `sample_id, input_fingerprint, status, error_code, result`；失败的 result 为 null。
本次范围为 genshin_voice、starrail_voice、wutheringwaves、zenless_voice 的已发布固定版本，
不修改 base 的 language，也不覆盖 transcription 的 language 或已有 annotation。

采用本地 FireRedLID 权重和官方 AED 推理实现，FP32、关闭 TF32。profile 固定权重、CMVN、词表
SHA256，官方代码 commit 与实际文件 SHA256、前处理/解码配置、运行实现与依赖版本。
不使用来源语言或 ASR 语言提示模型，不限制输出只能为来源语言。

| result 字段 | 含义 |
| --- | --- |
| audio_sha256、source_language | 输入编码音频身份、固定 base 原始语言（可空） |
| native_sample_rate、channels、decoded_num_samples | 实际解码采样率、声道数、完整音频每声道采样点数 |
| declared_num_samples | base 头信息，少量长度差异不构成失败 |
| start_sample、end_sample | 原生采样点半开区间；从 0 到 `min(decoded_num_samples, 200 * native_sample_rate)` |
| analysis_truncated | 是否只分析前 200 秒；true 时结果不能解释为已检查尾部 |
| model_sample_rate、model_num_samples | 16 kHz 模型输入的采样率和实际采样点数，不与原生坐标混用 |
| raw_language | 官方词表解码出的完整标签，如 `zh mandarin`；保留方言证据 |
| language | 第一标签规范化；`zh yue` → `yue`，`other` → `und`，旧代码 iw/jw/tl → he/jv/fil，其余保留第一标签 |
| confidence | 官方 beam 第一候选的已选语言 token 概率算术均值，保留未四舍五入的值，范围 [0,1]；不是校准过的正确概率 |
| source_language_mismatch | 与来源语言小写主标签比较；来源为空或输出 und 时为 null。只是冲突证据 |

**不做 30 秒切分**。200 秒以内完整输入，超过 200 秒明确裁取前 200 秒；原音频保持不变。
libsndfile 解码 float32，多声道算术均值；先按原生坐标取分析范围，再用 SciPy resample_poly
默认 Kaiser(5.0)/constant 边界重采样到 16 kHz，浮点幅值乘 32768 后沿用官方 80-bin fbank 和 CMVN。
不做波形补零、VAD、静音删除或长度归一化。少于一个 25 ms 特征窗的输入记为
`failed/audio_too_short_for_fbank`，不补零凑帧。
按特征长度分桶，使用官方有效长度 mask 的特征批处理及模型原有卷积边界处理；
须通过与单条 FP32 的数值对照后启用，批大小/帧预算/GPU 并发在 execution 中记录。
显存不足先减小 batch，单条仍不能处理则停止排查，不把系统故障批量标记为坏样本。

解码错误、空音频、非有限波形或无有效特征逐条记 failed；低置信度、und 和语言冲突仍可为 ok，
selection 再决定使用方式。此模型会对非语音给出标签，本任务没有确认“存在语音”的语义。
单条非法标签/非有限模型置信值分别记为 model_invalid_language/model_invalid_confidence；
模型加载/配置/身份冲突、整批至少 4 条均输出非法结果、整段至少 32 条均失败等系统性异常停止阶段。
不能回退到来源语言或 ASR 语言。

input_fingerprint 为规范 JSON 的 SHA256，包含 sample_id、audio_sha256、
`analysis=first_min_duration_200s`、source_language 和 profile_id；source_language 参与指纹，
因为输出还保存来源对照证据。无关文本和 ASR 输出不参与模型输入或指纹。
先固定完整任务计划，检查点核对每条 ID、结果 schema 和完整回读；恢复时核验输入、模型、代码及检查点摘要。
对外只发布一张 results.lance，不创建 targets.lance。所有数据集完成覆盖、全字段回读、索引和版本 tag 后，
最后发布全局 complete manifest，训练/selection 不得把私有检查点当成已发布结果。

## MOSS 说话人结构单表

`speaker_structure` 的 `moss-transcribe-diarize-v1` profile 使用 sample_table：每个固定 base 样本恰好一行，
顶层 `sample_id, input_fingerprint, status, error_code, result`。范围为 emilia、emilia_yodas、emilia2 已发布固定版本的
全部样本（all_base_samples）。说话人区间在单个样本内有界、总与摘要一起读取，按[存储边界](#存储边界)放在 result 的列表中，
不建 targets/results 双表。不修改 base 的 speaker_id、text 或 language，也不覆盖已有 annotation。

推理由外部服务完成：OpenMOSS `MOSS-Transcribe-Diarize`，由 SGLang Omni 部署的 OpenAI 兼容
`/v1/audio/transcriptions` 接口（`response_format=verbose_json`）。本仓库是客户端，本地不解码音频。profile 固定：
- 模型 ID、服务方提供的权重 revision 或摘要（未提供为 null，不推断）、SGLang Omni 版本与启动参数（服务方提供）；
- 请求协议与生成参数（温度、最大输出长度等），不提供 prompt 或热词，不指定语言，不使用 base 文本或其他 annotation；
- 提交方式：每个样本一次完整提交 base 原始编码音频字节（不重新编码、不裁剪，请求音频即 audio_sha256 所指字节）；
- 时间映射、取整、按音频格式的越界容差（含服务端解码器与 base 读取器的起点偏移，如 MP3 编码延迟），标签规范化与排序规则；
- 模型可表示的说话人上限（未知为 null），以及是否输出相互重叠的区间（`overlap_representation`）；
- 判为"拒绝该样本"的 HTTP 状态与错误类别、重试策略、并发数，以及结果是否随服务端批处理变化。

具体响应字段、各格式的起点偏移与容差、服务端是否接受三个 base 中的全部音频格式，以试点对该服务的实际探测为准，
冻结进 profile 后才能全量运行；协议或服务变更视为新 profile。

| result 字段 | 含义 |
| --- | --- |
| audio_sha256、sample_rate、num_frames | 提交的音频身份；base 固定的原生采样率与读取器帧数，作为坐标范围 |
| speaker_count | 至少有一个区间的本地说话人标签数；估计值 |
| turn_union_samples | 全部模型区间的并集长度（原生采样点）；区间来自端到端转写，不等同 VAD 语音时长 |
| overlap_samples、overlap_ratio | 区间两两重叠部分的并集长度及其 / num_frames；模型不输出重叠区间时为 null |
| dominant_speaker_ratio | 并集最长的说话人的并集长度 / turn_union_samples；没有区间时为 null |
| turns | list<struct{speaker, start_sample, end_sample, text}>，按 (start_sample, end_sample, speaker) 排序 |
| overlaps | list<struct{start_sample, end_sample}>，由 turns 推导的重叠并集；模型不输出重叠区间时为 null |
| response_sha256、response_bytes | 服务原始响应的摘要与字节数 |
| raw_response_json | 仅非 ok 行作为证据保存；ok 行为 null |

解释：
- speaker 按 profile 的规范化规则得到（如按首次出现记为 spk0、spk1…，并保留模型原始标签映射），只在该样本内有效，
  不写回全局 speaker_id，不与上游说话人比对。
- turns.text 是模型随区间输出的文本，只是证据，不是可采用的转写，也不参与本任务的任何判断。
- 时间：响应秒数映射为原生采样点 `start_sample = floor(start × sr)`、`end_sample = ceil(end × sr)`，再裁到 `[0, num_frames]`；
  超出范围在 profile 固定的格式容差内裁剪；超出容差、起点不小于终点、无法解析的响应记 `failed/model_invalid_output`。
- 模型只输出串行不重叠区间时，重叠没有被测量：overlap_samples、overlap_ratio、overlaps 为 null，不能当作零重叠。
  只有模型能表示重叠且未给出重叠时才为 0 和空列表。
- 没有任何区间仍为 ok：speaker_count=0、turns 为空、turn_union_samples=0、dominant_speaker_ratio=null。
- speaker_count 达到 profile 记录的模型上限只是事实，不能证明遗漏了说话人。

状态：
- 服务按 profile 固定的类别明确拒绝该样本（如音频无法解码）记 `failed/server_rejected_input`；
  响应不合规记 `failed/model_invalid_output`；生成达到最大输出长度（服务给出的结束原因，或无结束原因时最后区间远早于音频结束等
  profile 固定的判据）记 `failed/output_truncated`，不当作完整结果。
- 连接、超时、5xx、限流及未归类的 4xx 先按 profile 重试，仍失败时停止阶段，不能记成坏样本；
  服务拒绝某一音频格式的全部样本属于系统性问题，停止阶段。
- 运行中定期核对模型身份（模型列表响应摘要与服务版本），变化即停止；整段连续多条失败等系统性异常停止阶段。

原始响应很大（全部样本约数百 GB）且与解析字段重复，与 `qwen-asr-transcription-v2` 不同，ok 行不发布原文：
完整原始响应保存在任务私有检查点中，压缩归档为审计证据，不是发布产物。

input_fingerprint 为规范 JSON 的 SHA256：`speaker-structure-input-v1`、sample_id、audio_sha256、
`full_original_bytes`、profile_id。全量前试点须核验：同一样本重复请求、不同并发下结果是否一致，实际吞吐与预计总时长；
结果随服务端批处理变化时在 profile 记录并固定并发，不能让恢复混入两种行为。
先固定完整任务计划，原始响应按样本写入可恢复的检查点，恢复时核验输入、profile、代码与检查点摘要；
所有数据集完成覆盖、全字段回读、sample_id 索引和版本 tag 后，最后发布全局 complete manifest。
