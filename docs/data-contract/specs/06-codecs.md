# 06 · Codec 与音频特征的共同约定

三表独立提取及身份关联见 [13 独立文本表](13-text-features.md)。训练专用构建由训练仓库管理。

本章固定 codec 和 speaker embedding 共同的存储、关联和发布规则；speaker 的两条训练路径见
[11 Speaker embedding](11-speaker-embeddings.md)。annotation 的结果与依赖规则见 05。
基础 27 列和 release v0.1 不变；特征类型独立，不把某一训练模型的形状写入 base。

## 1. 发布单元与多版本

```text
datasets/<dataset_id>/v0.1/
├── samples.lance/
└── features/
    ├── codec/<run_id>/{manifest.json,features.lance/}
    └── speaker_embedding/<run_id>/{manifest.json,features.lance/}
```

一个 run 属于一个 dataset/release、一个 kind、一个 profile、一种 target_kind（sample）。
每个 run 发布一张自包含 Lance 特征表，内部可有多个 fragment；不按 GPU/batch 建公开目录，
不为每条音频建立 NPZ，不复制原音频。一个 dataset 可拥有多个 codec 和 speaker profiles。
相同 profile 可以用于多个 dataset，分别发布；训练所需的跨 dataset 组合由训练端管理。
selection_branch 模式直接引用已发布的选择分支，不复制目标表；额外任意 subset 才在本 run 保存 targets.lance。

目录不再增加 profile_id 哈希层；run_id 已包含可读 profile_name 和时间/序号。
profile_id 仍保存在 manifest 与特征行，参与 feature_key/input_fingerprint，是兼容性校验所需身份。
同名 profile 不保证相同配置，读取必须核对完整 profile_id，不能只看目录名。
历史已发布的哈希层布局仍按 manifest 路径读取，不重命名已被引用的发布目录。

- profile_id：完整处理定义的 canonical JSON SHA256，决定结果含义。
- run_id：一次固定输入、固定选择范围的执行；重试不改变 run_id，发布后不可改写。
- Lance version：实际表的整数快照，manifest 明确固定；run_id 不代表 latest。

新的 feature run 使用可读 ID：`<dataset_id>-<kind>-<profile_name>-<北京时间>bjt-<序号>`，
例如 `libritts_r-codec-qwen3-12hz-24k-k16-fp32-v1-20260929T200000bjt-01`。
dataset_id 原样保留（包括下划线），不替换为连字符；音频 kind 为 codec 或 speaker_embedding；独立文本使用 text（见 13）。
profile_name 为 1–128 字符，匹配 `[a-z0-9][a-z0-9_-]{0,127}`，在命名中原样使用。
北京时间（UTC+08:00）格式为 YYYYMMDDTHHMMSS，路径追加小写 bjt，不使用 `+`、空格或冒号。
manifest 时间仍使用 ISO8601 `+08:00`；路径时间仅用于可读命名，不能替代 manifest 的时间字段。
序号为正整数，至少两位十进制，不多余补零；同一命名空间发生同秒重名时原子分配递增序号。
时间表示首次创建；同一次执行的重试沿用原 ID，不覆盖已有目录。
命名生成器只格式化名称，不分配目录；发布器仍须原子占用名称。
时间不决定数据有效性或输入版本；历史已发布 ID 保持原值。
manifest 增加必填的可读 profile_name，例如 `qwen3-12hz-24k-k16-fp32-v1`，与完整 profile_id 一起展示。
profile_name 是说明性标签，放在 profile 对象之外，不参与 profile hash，不能用于唯一性、兼容性判断或替代完整引用。
同名不同 hash 必须显示为不同 profile；状态输出展示 dataset、profile_name、run_id、状态与精确路径。
sample_id、feature_key、profile_id 的哈希算法不变；原来源名称仍从 source_key/locator 查看，不给稳定内容身份加入时间戳。

profile.kind 区分 audio_codec 与 speaker_embedding。换权重、影响输出的实现、前处理或精度均产生新 profile。
同权重而不同重采样/切片策略仍是不同 profile；仅码本数相同不代表训练兼容。
消费者须明确选择各类特征的 profile；同 profile 可组合不同 dataset/run。
混合不同 profiles 必须在模型协议中声明并验证，不能按模型名称或张量形状自动混合。

## 2. Profile 和运行参数分开

生产 profile 必须固定并核验：

| 内容 | 必须记录 |
| --- | --- |
| 模型 | 架构/variant、完整 config、实际使用权重文件/张量摘要、上游 revision |
| 实现 | encoder/decoder 或 speaker frontend 的代码 revision/摘要、影响数值的依赖版本 |
| 时间轴 | timeline profile 的完整定义与 ID，原生采样帧解释、解码器与长度处理 |
| 波形处理 | 完整样本消费、声道合并、重采样实现/参数/长度规则、幅度处理、静音处理 |
| 推理 | eval、精度、autocast/TF32、随机性、长度/padding/mask、分块与上下文策略 |
| 输出 | codec 的 K/逐码本 vocab/轴/dtype，或 embedding 的 D/dtype/池化和归一化 |

profile 不包含自身 ID、run_id、输入 dataset、机器路径、时间戳或 GPU 编号。
完整 profile 内含 timeline 定义，timeline_profile_id 必须与其 canonical hash 相符。
选择清单、机器/GPU 型号、驱动、实际执行的 batch 统计、耗时、GPU 数量和 worker 数放 run manifest/execution。
决定单条结果的 batch 形状/上限/补齐规则仍必须在 profile 固定，不能只写在 execution。
不能将影响数值的 batch/padding/backend 差异仅标为性能参数；如未证明批处理不改变每条结果，
应使用逐条基线或把确切处理语义纳入新 profile，不静默混合旧缓存。
worker 数和调度顺序不参与业务 ID；profile 声明 per_target_independent 时必须按以下数值比较规则验收不同 batch 组合。
profile.reproducibility 固定比较器：codec 使用 exact_integer；speaker 使用 elementwise_atol_rtol，
atol/rtol 为已实测验收的有限非负数。容差不能任意放大以通过验收；额外余弦/范数标准也须固定在 profile。

profile_id 只对完整 profile 对象计算，不包含示例包装中的 example_only/runnable/note。
生产 profile 不允许未解决的 null/占位符。无操作明确写 none。仓库中的候选模板 runnable=false，
不以模板 hash 冒充已验收的模型 profile。实际权重与前处理验证完成后才生成生产 ID。

## 3. 输入、时间轴和关联

生成时只读取 complete manifest 指定的样本 snapshot，不接受转换中的目录。
基础表负责保留音频，特征表只保存结果和指纹。run inputs 固定 manifest SHA256、table_path、branch（main=null）和整数 snapshot。
selection_branch 输入另外绑定 selection manifest SHA256；相同整数版本在不同分支不代表相同数据。

| 公共字段 | 语义 |
| --- | --- |
| target_kind / target_id | sample；run 内 target_id 唯一 |
| parent_sample_id | 等于当前完整输入样本的 target_id；原始裁剪来源由 selection 记录 |
| profile_id | 与 manifest 的完整 profile 摘要一致 |
| audio_sha256 | 原始编码 audio.bytes 的 SHA256，绝不以路径代替 |
| timeline_profile_id | 本行实际使用的原生解码时间轴 |
| native_sample_rate | 该原生时间轴的 Hz，int32 > 0 |
| start_frame / end_frame | int64 原生采样帧半开区间，绝不是 codec 帧 |
| encoder_input_num_frames | 前处理后送入 encoder/frontend 的有效波形采样帧，排除 batch padding |
| encoder_input_sha256 | 上述有效波形的规范化摘要，不是原始文件摘要 |
| feature_key | 音频内容、时间区间、profile 的可复用计算身份 |
| input_fingerprint | 本次 target 与该计算输入的绑定，公式见下 |
| status / error_code | ok / failed / unsupported / skipped；非 ok 必须说明原因 |

整条音频模式的 sample 任务完整解码当前样本，成功行区间为 `[0, 实际解码帧数)`；
核对采样率、声道、有限值和有效输入长度，不为匹配头信息补零或裁尾。
已固定的旧 codec profile 仍可能要求解码长度与 base.num_frames 相等并在不符时失败；
这属于旧运行限制，不是所有特征的通用拒收规则。不能为修改文档而改写旧 profile 或成功结果。
输入区间相对于当前完整样本，从零开始；不能把裁剪来源的父录音坐标再次应用到片段。
错误行保留计划中的父引用、音频哈希和区间；无法确定有效区间的对象必须先解决时间轴，不能假装完成编码。

特征提取顺序：完整解码当前样本 → 明确声道策略 → 重采样 → encoder/frontend。
需要裁剪时已由 selection 生成实际片段；特征任务不再应用父音频中的裁剪区间。
完整录音先编码再切 token 不等价于波形先裁剪再编码；不允许由时间比例猜 token 边界。
长音频若超过已验收容量，标 unsupported 或由 selection 先生成实际裁剪样本；不偷偷截前 N 秒。
改变训练音文范围的裁剪样本在 selection 阶段生成，不能将其冒充原始整条 sample 的特征。
显式 reference speaker 模式是例外，按 [11](11-speaker-embeddings.md) 固定子区间并保留原 sample 身份；
其 start_frame/end_frame 描述实际 reference，完整 codec/text 保持不变。上面的整条输入顺序不适用于该模式。

```text
feature_key = SHA256(canonical_json(["feature-v1", audio_sha256, timeline_profile_id,
                                   start_frame, end_frame, profile_id]))
input_fingerprint = SHA256(canonical_json({
  "task": "audio-feature-v1", "kind": kind, "target_kind": target_kind,
  "target_id": target_id, "parent_sample_id": parent_sample_id,
  "audio_sha256": audio_sha256, "timeline_profile_id": timeline_profile_id,
  "native_sample_rate": native_sample_rate, "start_frame": start_frame,
  "end_frame": end_frame, "profile_id": profile_id
}))
```

kind 在以上公式中为 codec 或 speaker_embedding。profile 包含对应的 profile.kind。
纯音频特征的指纹不包含 text、speaker 标签或整个 record_revision，文本修订不会使其无意义失效。
run manifest 仍固定读取的确切快照。跨快照复用须重新核对父音频、坐标与处理 profile。
feature_key 相同的不同来源 target 可以各有一行；首版不引入额外去重映射表。
同一 run 中每个 feature_key 只有一个权威成功结果：协调者选取第一个通过完整验证、持久化并提交检查点的结果，
随后所有同 key target 复用其原始数组与摘要。并行计算不是多个结果都可发布；恢复先验证该权威结果的精确摘要。
在复用或比较前，必须核对 profile、feature_key 和 encoder_input_sha256 完全相同；波形摘要不同是输入冲突。
独立重算的候选不能覆盖已提交结果：codec 必须逐整数相等；speaker 的每个元素必须满足
`abs(candidate - canonical) <= atol + rtol * abs(canonical)`，用 float64 比较、权威结果为相对容差基准。
容差内保留原权威结果并记录重算验收数量；超出容差或输入冲突必须隔离并阻止发布，不能记为普通单条 failed 来绕过。
因此同一 run 发布的同 key 成功行仍具有完全相同的 payload 摘要；数值等价不替代磁盘完整性校验。
不同 run 的同 key 浮点结果可能摘要不同，消费者必须固定实际 run/snapshot/摘要；跨 run 复用也须验证来源，不能查 latest。

### 输出等价的实现升级

上面的同键复用不允许暗中更换 profile。仅优化执行、保持 codec 整数输出的实现升级，
可以通过显式迁移建立新 profile/run，并接续已完成的结果。必须固定旧/新 profile、来源 plan、
等价验收证据及其 SHA256；模型权重、依赖、前处理、区间、精度、attention 语义和输出空间保持一致。
验收覆盖跨卡、重排、重试、长度边界及真实完整流水线；浮点接近或主观听感接近不能替代整数一致。

迁移先核验来源 checkpoint 和全部文件摘要，再按新 profile 重算 profile_id、feature_key、
input_fingerprint；其余身份、输入波形摘要、codes 和 codes_sha256 不变。新文件必须完整读回核对，
新 checkpoint 记录旧 profile、来源 checkpoint/文件摘要及等价证据；发布 manifest 汇总复用行数与来源。
旧结果保留至新检查点完整验证、恢复运行确认之后再清理，保留迁移审计记录。
改变 FP32/FP16、attention 窗口或已知会改变码字的批处理路径，不能使用本迁移通道。

## 4. Codec 的具体列和序列化

公共字段之外：

| 字段 | 类型与约束 |
| --- | --- |
| num_codec_frames | int64，成功时 T > 0，取实际返回的有效序列长度 |
| num_codebooks | int32，固定为 profile 的 K，错误行也填写 |
| codes | list<fixed_size_list<integer,K>>，形状 [T,K]，外层 time，内层 codebook |
| codes_sha256 | 成功时必填，规范数组内容摘要 |

每个 codebook 值域分别为 `[0,vocab_sizes[k])`。存 int16 或 int32，profile 固定一种，
容量必须覆盖实际值域。Qwen 候选用 int16；进入 PyTorch 模型时转换为 torch.long。
不得先强制转整数再验范围；编码器输出必须先检查整数类型、负值、上界、形状与有效长度。
不在持久化 codes 里加 BOS/EOS/PAD、文本 token 或语言 token；这些由模型输入协议处理。
不持久化 batch padding，也不按“12Hz × 秒数”猜 T。encoder 的长度裁剪规则必须从实现核实并验收。

非 ok 行 codes、codes_sha256、num_codec_frames 为 null。
前处理成功但推理失败，可保留 encoder_input_*；尚未形成有效输入波形则这两个字段为 null。
不得用零长度数组、全零 token 或上一条结果冒充失败。

所有数组摘要使用：

```text
SHA256(canonical_json({"dtype": dtype, "shape": shape, "axes": axes}).encode("utf-8")
       + b"\n" + little_endian_C_contiguous_array_bytes)
```

codes：dtype=int16/int32，shape=[T,K]，axes=[time,codebook]。
encoder_input_sha256：对实际前处理结果的 float32 单声道有效波形计算，shape=[N]，axes=[sample]，
发生在 encoder 内部 padding 或混合精度 cast 之前；非单声道 profile 必须另行显式定义输入形状。
hash 不包含 NPZ 文件包装、Lance 编码或输出路径；这些物理文件另有完整文件 hash。

## 5. 模型特定配置与验收边界

契约不规定唯一 tokenizer、精度、attention 后端或固定 batch 大小；这些属于实际 profile。
声称采用某种后端时，必须核对实际执行模块/调用，不能只凭配置字符串或静默回退后的名称。
模型的有效帧率、输出长度和 padding/mask 必须按实际实现验证，不能按产品名称猜测。
量化码本、惰性缓存、残差与距离计算的实际精度和初始化顺序也属于 profile；不能只记录参数 dtype。

任何影响数值的变更创建新 profile，并分别验收；不同 profile 的结果不要求整数相同，
但同一 profile 在其声明允许的调度/批处理组合下必须满足第 2–3 节的复现规则。
若 profile 固定计算形状，OOM 或重试不得偷偷改变形状；不支持的输入按固定失败策略处理。
本次部署选择及其硬件/性能证据记录在 pipeline 的 docs/codec.md 和实际 run/profile 中，
不把一次实验配置提升为所有未来 codec 的存储规范。

## 6. 索引和高吞吐读取

### 已有 codec 运行的兼容文本列

已有冻结 codec 执行器在音频特征列之外物化以下选用元数据，保留该输出以兼容当前运行。
独立 text 表是后续合表选定的文本来源，见 13；不要求新音频执行器重复保存这些列。
这不改变纯音频特征身份；`profile_id`、`feature_key`、`input_fingerprint` 和 codes 摘要不包含文本。

| 列 | 类型 | 含义 |
| --- | --- | --- |
| text | string，非 null | selected_text 非 null 时取它，否则取基础 text；必须非空白，不用真假值判断回退 |
| language | nullable string | selected_language 非 null 时取它，否则取基础 language；不额外猜语言或改 alias |
| text_kind | nullable string | 基础文本类型；若以后修订改变类型，必须先明确新映射 |
| text_source | uint32，非 null | 对应 selection.text_sources；0 为基础文本/规范化，修订源使用已登记码值 |
| text_revision | string，非 null | 沿用 05 的 selected-text-v1，绑定所选原文或修订及规范化定义 |
| dataset_id | string，非 null | 原始来源标识，用于混合训练采样与诊断 |
| source_key | nullable string | 来源内可解释身份，不替代 target_id |
| speaker_id / speaker_scope | nullable string | 复制已知说话人及其作用范围；未知保持 null，不伪造身份 |

`target_kind=sample` 时 target_id 就是 sample_id，不必再增加一列相同 ID。
duration 可由 `(end_frame-start_frame)/native_sample_rate` 求得，不必复制音频或 metadata_json。
首版不保存 text token_ids：它们依赖训练文本 tokenizer 和协议，未来缓存须独立固定 tokenizer/config。
speaker embedding 表不复制这份文本；三表按样本身份关联，训练端决定自己的读取布局。

使用此兼容物化流程的 codec manifest 必须有 `text_materialization`：模式 `selection_text_columns_v1`、列名、覆盖行数、
有序 `(target_id, 元数据)` 回读摘要、text_sources，以及是否保持原 codec 文件。
选用来源由原有 selection manifest SHA256、分支和版本固定；不接受只有当前文本字符串的无来源快照。
新增列纳入真实 schema_sha256；文本摘要与 codes 摘要分开验证。
有序文本摘要为：按固定 feature 行序，对每行 `canonical_json([target_id, metadata_dict]) + "\n"`
的 UTF-8 bytes 连续计算 SHA256；metadata_dict 恰好包含上表九列，null 保留，字典键排序。
物化与回读分别计算该摘要，必须相同；示例见 [codec text](../examples/codec-text.example.json)。

允许在尚未发布的 features.lance 通过 add_columns 增列，只写新列文件并复用 codes 文件。
必须逐条验证目标 ID 对齐，校验物化后的 text/language 与固定 selection 完全一致。
禁止仅根据两个扫描器“碰巧顺序相同”推断对应关系；不能在 callback 中读正在被可变借用的同一表句柄。
中断恢复必须重新核对来源与输出，禁止把部分完成的文本列当成可训练发布。

发布后文本修订应产生新的固定特征快照/run 或训练派生分支，更新文本摘要与 manifest，
原有被引用版本保持不变。只写新增/替换列，复用兼容的 codes 文件；不重新跑 tokenizer。
如果扩充到旧表没有的音频目标，新增目标的 codec 仍需补算。
全库文本及身份字段有实际存储成本，约数十 GB 未压缩量级，需用真实分片统计；不能声称零开销。

### 定位

features.lance 必有 target_id、feature_key 的 BTREE。
parent_sample_id 在固定 base snapshot 的 sample_id 索引上查找。
按 ID 取少数结果无需把所有文件 load 进内存。批量全库生成使用顺序/分片扫描，
投影所需列；构建训练数据时批量关联，可用本地盘外部排序/SQLite，禁止逐行远程随机查询。
索引不保证唯一性和外键，发布时必须显式验证。

结果乱序写入可以接受，但消费者必须按样本身份核对对应关系，不靠相同行序。
训练专用定位、采样和读取布局由训练端管理；交付边界见 12/13，在线 speaker 路径见 11。

## 7. 生成、失败、续跑和验收

1. 固定 inputs/profile/target_kind；首轮使用 selection_branch，消费完整发布的 supervised_tts selection。
   all_samples 只适用真正全体目标。任意额外 subset 固定 targets.lance；
   所有模式都在推理前校验目标集合摘要，不接受只有易变查询字符串的范围。
2. 按目标 ID 与 input_fingerprint 建有界任务；CPU 解码、GPU 推理、Lance 写入分工。
   每张 GPU 的进程/队列数显式有界，按验收配置执行；CPU 解码线程、GPU 进程与模型 batch 是不同参数。
3. GPU worker 只返回已校验结果；单一协调者提交同一张表。并行 worker 写未提交 fragment，
   只有已写回、哈希和读回验收成功的 checkpoint 可恢复。
4. 调度/checkpoint 键使用目标与指纹，不以 worker/rank/物理行号作为身份；保存 code/dependency/profile 摘要。
   改 workers 不重新编码已验证目标；改 profile 新开 run。未提交 orphan 在核对可达性后才清理。
5. 工作目录位于 dataset/.state/<release>/features/<kind>/<run>/；
   共享盘只保留小状态和输出。身份唯一性审计 SQLite 放本地 TMPDIR，批量写入、最后建辅助索引，完成删除。
6. 生成到 `<run_id>.incomplete/`，检查 coverage、shape/range、所有输入引用和有效输出 hash、索引、
   固定 snapshot 可读，再原子发布 `<run_id>/`。不修改 samples.lance 或其他已发布 run。

published rows = selection.target_count = coverage.total_targets = ok + failed + unsupported + skipped，missing=0。
complete 表示任务范围完整记账和存储验收完成，不等于全部成功；仅 ok 行可用于该特征的训练。
未尝试/进程中断仍留 incomplete，不伪造 skipped；skipped 必须由固定的显式策略决定。
记录每种 error_code 数量、成功输入时长、失败覆盖与校验范围；不能以“表能打开”作为全部验收。

同一未发布 run 可对失败目标重试，最终只保留一个结果。已发布 run 不覆盖；同 profile 补算
优先仅对缺失/失败/新入选目标发布 subset run，不复制已有成功数组。新 run 对自身目标集合完整记账。
消费者可以显式引用多个兼容 run，固定每个目标选中的 run 和 snapshot，不得查询 latest 补洞。
不同 profile 不作为兼容补算；全量重新物化已有成功结果需明确空间预算，不能默认每次补洞复制全库。

特征提取/发布验收包括：多采样率/声道/长度/语言、空或损坏音频的处理、batch padding/重排、
长音频边界、真实形状/长度/值域、错误处理与覆盖记账、断点/换 worker、Lance 回读与按 ID 查询。
codec 还需编码再解码抽检、听检及有效帧数记录，不能把重建保真度当作原始语音质量评分。
音色克隆、训练 loss/梯度与真实训练读取吞吐由训练端验收，不阻止已验收的纯特征提取。
fail-closed 执行器可以在任何输入/模型错误时停留 incomplete，不必伪造失败行后宣称 complete；
如果允许失败行发布，则必须验证明确的错误策略与全部目标终态计数。
实现与验收状态由 pipeline 记录；满足本章提取门槛不等于训练读取已经就绪。

## 8. 选择范围与 targets.lance

selection 固定 mode、input_alias、available_target_rows、target_count、target_set_sha256。
input_alias 指向 inputs 中固定的目标样本表。
available_target_rows 是该目标表指定 snapshot 的总行数。
all_samples 要求 target_count 等于 available_target_rows，不创建 targets.lance；
subset 要求 `0 < target_count <= available_target_rows`，允许显式列举全体。空选择不发布 feature run。

subset 的 selection.table 必含 table_path（相对 release 根的本 run targets.lance）、branch（main=null）、lance_version、
schema_sha256、rows；rows 等于 target_count。类型见 schemas 中 feature_targets：
target_kind、target_id、parent_sample_id，均为非 null string。一个 run 只允许一种 target_kind 和一个目标表别名。
sample 的 parent_sample_id 等于 target_id；裁剪产物的原始来源由 selection 追溯。
发布前全量核验目标唯一、存在、父引用正确，features 的目标集合与选择集合精确相等，不能只比较行数。
targets 建 target_id BTREE；各个目标样本独立核验，不能按同一来源合并任务。

target_set_sha256 对按 (target_kind,target_id) 字符串升序排列的唯一目标做流式 SHA256：
每行是 `canonical_json([target_kind,target_id,parent_sample_id]).encode("utf-8") + b"\n"`。
all 模式也计算同一摘要；它与固定输入 snapshot 共同定义范围，不受扫描顺序影响。
选择表在推理前验证并写入执行计划；恢复必须匹配原摘要和 snapshot。发布后两个表都不可改写，均为保留根。
示例见 [样本子集 manifest](../examples/feature-subset-manifest.example.json)。


## 9. 直接消费 selection 分支

`selection.mode=selection_branch` 限 target_kind=sample；固定 input_alias 指向的 samples branch/version，
另有 manifest_path/manifest_sha256 指向 complete selection，dataset_id/release_id 与目标 run 一致。
filter 固定为 `selection_reason = 0`，不能在此隐式追加另一套文本/质量/语言策略。
available_target_rows 是该分支全部基础行数，target_count 是 reason=0 条数；不创建 targets.lance。
target_set_sha256 沿用第 8 节按 ID 排序的摘要，执行前与发布时均核验；外部排序可用本地临时盘，不保存第二份常驻全量目录。
引用者须核对该分支正是 selection 输出，规则/基础 manifest/快照完整一致，不能只相信 filter 和行数。
新 feature run 对未选入样本不记 failed/skipped；它们在 selection 有自己的原因。只有目标集合内需要完整终态记账。
生产训练用 selection 的文本条件只决定本轮要算哪些样本，不进入纯音频 feature_key。

小规模 pilot 若从 selection 再抽样，mode=subset，保存少量 targets，同时在 inputs 固定父 selection 分支与 manifest。
抽样规则/seed/数量记 execution；这种任意子集不能伪称消费了整个 reason=0 集合。
具体执行器支持的选择模式及其验收范围由 pipeline 记录；格式定义不能替代实际验收。
