# 05 · Annotation 分支、输入依赖与结果状态

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
| 每样本多个事件、字词对齐、多个候选 | 独立 results.lance + targets.lance，storage_kind=result_table |
| 文本纠错、重新转写 | annotation 分支的文本修订 struct，明确 keep/replace 等状态 |
| 改变实际音频区间、切分 | 由 selection 产出实际片段；不能只改 annotation 来冒充新的音频输入 |

极稀疏或大体积的一对一结果也可显式选择 result_table，manifest 固定布局和原因；
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
按target_id建BTREE辅助查询，索引不提供唯一性或外键保证；一对多不使用基础行位置直接对齐。

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
生产执行器与自动对齐验证器尚待实现，存储实验通过不等于全量annotation已就绪。

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
