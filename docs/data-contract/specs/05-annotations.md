# 05 · Annotation 分支、输入依赖与结果状态

独立结果表与训练质量档案的新设计见 [Annotation 实验契约](../experiments/annotation-v1.md)。
该稿用于试验，尚未替代本章默认布局或现有生产 schema。

## 存储边界

一对一、较密集的样本标注默认写入 samples.lance 的独立 annotation 分支。
annotation 与 selection 都直接从固定基础 main 版本 bv 建立；不把 selection 建在 annotation 上，
也不通过继承上一份 annotation 来组合任务。基础 27 列、main 和基础 manifest 保持不变。

```text
samples.lance（main，固定 bv）
├── ann/<task>/<run_id>        # 只增加本任务结果列
├── ann/<other_task>/<run_id>  # 同样从 bv 建立
└── <selection_id>            # 从 bv 建立，组合固定的 annotation 结果
```

分支名由引擎管理，不手工创建 tree 目录。task 使用小写字母/数字/下划线，不含路径分隔符。
run_id 使用 `tts-ann-<task>-YYYYMMDDTHHMMSSbjt-NN`，首次创建的北京时间、至少两位正整数序号；
重试沿用原名，原子占用避免重名。列名使用固定任务名或指标名，不包含 run 编号。
分支隔离不能代替任务输入校验；sample_id 仍是业务身份，位置只是可验证的读取优化。

| 结果类型 | 存储 |
| --- | --- |
| 每样本一个质量/识别/置信度结果 | annotation 分支的任务 struct 列，storage_kind=sample_branch |
| 每样本一条、独立保存状态与可空结果 | 单张 results.lance，storage_kind=sample_table |
| 每样本多个事件、字词对齐、多个候选 | 独立 results.lance + targets.lance，storage_kind=result_table |
| 文本纠错、重新转写 | annotation 分支的文本修订 struct，明确 keep/replace 等状态 |
| 改变实际音频区间、切分 | 由 selection 产出实际片段；不能只改 annotation 来冒充新的音频输入 |

独立的一对一样本标注可显式选择 sample_table，状态和结果保存在同一行，不另外创建 targets 表。
已有 result_table 的一对一任务保留兼容读取，迁移必须明确 schema/layout 版本；
manifest 固定布局和原因；
不强制为少量结果创建全行结果列。稀疏空列有 bitmap/元数据开销，不承诺零存储。
旧 storage_kind=sample_column 与 ann__task__run 列名只作兼容读取，新任务不继续向 main 追加这些列。

## Run 发布与多数据集

单数据集和跨数据集任务均由一份 `annotations/<task>/<run_id>/manifest.json` 发布。
分支实际位于各 dataset/release 的 samples.lance 内；大结果表仍归所属 release：
`datasets/<dataset_id>/<release_id>/annotations/<task>/<run_id>/{results.lance,targets.lance}`。
不另造一份带基础音频/文本的全局表，不要求每个 dataset 再写重复的子 manifest。
同一跨数据集 run 在各表使用相同分支名，manifest 的 outputs 按 dataset/release 唯一列出固定版本。
所有计划输出齐全、校验和保护 tag 完成后，才原子发布全局 manifest；中途失败不发布部分 complete。

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

## 独立 ASR 转写结果

`qwen-asr-transcription-v2` 使用 sample_table，独立保存全量转写证据、原始响应及语言冲突候选，
不修改基础表。单张 results.lance 每个 sample 一行：sample_id、input_fingerprint、status、error_code、result。
成功行 result 非空，失败行 result=null；成功但 text 为空字符串与失败必须区分。
主转写及指定语言候选是同一个 result 内的字段，不为候选增加样本行。
历史 `qwen-asr-transcription-v1` 的 targets/results 双表保留兼容，item_id 固定为 0。
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

## 上游质量导入与采样

Emilia/YODAS：`json.loads(metadata_json)["upstream"].get("dnsmos")` → upstream_dnsmos列。
Wenet：`json.loads(metadata_json).get("upstream_dnsmos_p808")` → upstream_dnsmos_p808列。
可发布upstream_quality任务，两个指标分别使用struct列/指标profile；明确各列的适用dataset和选择范围。
只接受非bool的有限数值，固定源字段路径、来源快照与解析规则；未知模型版本记unknown。
这属于导入，不声称本地重算；未经校准不混用阈值。初版selection不依赖质量时无需先导入。
按投影批次读取metadata，不加载audio；空间统计包含struct/指纹/索引，不能只按一个float估算。

annotation保留测量事实；selection固定资格/原因和采用结果；训练端管理质量到权重的映射、语言比例与预算。
可按需要在 selection 或下游消费表中投影少量选用分数并记录来源，避免训练反复解析JSON；不将某次实验权重变成selection默认列。

## 生命周期与保留

全部annotation manifest及其传递依赖纳入保留根。selection引用的annotation版本/表/保护tag不能删除或清理；
独立重跑不表示可以删除仍被引用的旧run。复制了selected_text也不自动解除审计依赖。
基础增加数据需新release/明确版本，旧分支继续服务旧bv；新bv重新建立分支和定位关系。
音频/文本输入指纹未变的目标可经核验复用旧结果，不要求重跑全部模型推理。
改变bv不能直接沿用旧行位置；保护/回收要求见 [09](09-lance-operations.md)。

## 独立音频统计结果

`audio-stats-v2` 的 `audio_stats` 使用 sample_table，单张 results.lance 保存所有目标的状态与可空 result，
不改变 samples main。成功和失败都各占一行；顶层字段为 sample_id/input_fingerprint/status/error_code/result。
旧 audio-stats-v1 双表仅保留已有试验产物的兼容语义。本次全量在首次发布前迁移为 v2，复用检查点、不重算统计。
运行固定所有纳入的已发布 base 快照；未发布数据不在过程中加入。

`native-waveform-stats-v1` 不裁剪、不重采样、不混合声道、不补零；libsndfile 分块解码 float32，
统计使用 float64 累加，模型依赖为空。profile 固定 libsndfile、NumPy、Arrow、Lance 和代码版本。
result struct 保存以下逻辑字段，具体 Arrow schema 及摘要写入 manifest：

| 字段 | 定义 |
| --- | --- |
| audio_sha256 | 实际编码音频身份；sample_id 和 input_fingerprint 位于顶层 |
| native_sample_rate/channels/decoded_num_samples/duration_seconds | 实际解码格式、每声道采样点数及秒数 |
| start_sample/end_sample | 完整原生时间轴 `[0, decoded_num_samples)` |
| declared_sample_rate/declared_channels/declared_num_samples | 固定 base 中的头信息，供对照 |
| num_samples_delta/header_format_matches | 实际采样点数减声明值；采样率与声道数是否匹配 |
| peak/rms | 所有声道的最大绝对幅值；所有声道样本平方均值的平方根 |
| near_full_scale_ratio | `abs(x) >= 0.999` 的声道样本比例 |
| at_or_above_full_scale_ratio | `abs(x) >= 1.0` 的声道样本比例；不事先限幅 |
| zero_ratio | 恰好为零的声道样本比例 |
| channel_stats | 按从 0 开始的声道索引，分别保存上述幅度指标及均值 dc_offset |
| low_energy_intervals | 低能量窗口的合并区间，原生采样点半开区间 |
| low_energy_ratio | 合并区间总采样点数除实际每声道采样点数 |
| leading/trailing/longest_low_energy_samples | 首、尾及最长低能量连续区间长度；没有则为真实的 0 |

低能量窗口从原点开始，长度 `max(1, native_sample_rate // 50)`，互不重叠；
最后不足一窗按实际长度计算，不补零。所有声道窗口 RMS 都不大于 `10**(-50/20)` 才标记为低能量，
相邻标记合并；定位精度是窗口级，不是逐采样点的语音活动判断。
幅值以解码浮点值的 1.0 为满幅基准；比例的分母固定如上，不混用秒数、窗口数和声道样本数。

这些是测量证据：近满幅不等于已确认削波，低能量不等于无语音，正常统计不等于训练合格。
libsndfile 可对部分编码缺陷容错，有限值解码成功不保证压缩码流无损坏；本任务不承诺定位所有解码警告。
空音频、非有限波形、解码异常分别记为失败；身份哈希冲突停止运行。
头信息差异单独记录，按实际解码统计，不因为少量帧差丢弃。持续性执行错误应排查后再恢复。

输入指纹为 `SHA256(canonical_json(["audio-stats-input-v1", sample_id, audio_sha256,
"full_actual_native_decode", profile_id]))`，不绑定无关文本。
每个检查点逐条核对固定 base 范围的 ID；全量覆盖继承经过唯一性验收的固定 base 身份，
通过固定 fragment/range 的无重复计划覆盖所有行。发布前完整回读所有结果字段，建立目标 ID 索引，
保护固定输出版本，最后发布全局 complete manifest。私有检查点不是生产读取入口。

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
