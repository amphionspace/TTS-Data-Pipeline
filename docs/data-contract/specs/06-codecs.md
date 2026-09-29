# 06 · Codec 与音频特征的共同约定

本章固定 codec 和 speaker embedding 共同的存储、关联和发布规则；speaker 的两条训练路径见
[11 Speaker embedding](11-speaker-embeddings.md)。本轮不扩展 annotation 设计。
基础 27 列和 release v0.1 不变；特征类型独立，不把某一训练模型的形状写入 base。

## 1. 发布单元与多版本

```text
datasets/<dataset_id>/v0.1/
├── samples.lance/
└── features/
    ├── codec/<profile_id>/<run_id>/{manifest.json,features.lance/}
    └── speaker_embedding/<profile_id>/<run_id>/{manifest.json,features.lance/}
```

一个 run 属于一个 dataset/release、一个 kind、一个 profile、一种 target_kind（sample 或 view）。
每个 run 发布一张自包含 Lance 特征表，内部可有多个 fragment；不按 GPU/batch 建公开目录，
不为每条音频建立 NPZ，不复制原音频。一个 dataset 可拥有多个 codec 和 speaker profiles。
相同 profile 可以用于多个 dataset，分别发布；跨 dataset 合并和选择放在训练 build。
子集 run 另含同目录的 `targets.lance/`，由同一个 manifest 的 selection.table 绑定；不是独立发布类型。

- profile_id：完整处理定义的 canonical JSON SHA256，决定结果含义。
- run_id：一次固定输入、固定选择范围的执行；重试不改变 run_id，发布后不可改写。
- Lance version：实际表的整数快照，manifest 明确固定；run_id 不代表 latest。

profile.kind 区分 audio_codec 与 speaker_embedding。换权重、影响输出的实现、前处理或精度均产生新 profile。
同权重而不同重采样/切片策略仍是不同 profile；仅码本数相同不代表训练兼容。
默认一个 build 为 codec、speaker 各选择一个 profile；同 profile 可组合不同 dataset/run。
混合不同 profiles 必须在模型协议中声明并验证，不能按模型名称或张量形状自动混合。

## 2. Profile 和运行参数分开

生产 profile 必须固定并核验：

| 内容 | 必须记录 |
| --- | --- |
| 模型 | 架构/variant、完整 config、实际使用权重文件/张量摘要、上游 revision |
| 实现 | encoder/decoder 或 speaker frontend 的代码 revision/摘要、影响数值的依赖版本 |
| 时间轴 | timeline profile 的完整定义与 ID，原生采样帧解释、解码器与长度处理 |
| 波形处理 | 声道合并、裁剪顺序、重采样实现/参数/长度规则、幅度处理、静音处理 |
| 推理 | eval、精度、autocast/TF32、随机性、长度/padding/mask、分块与上下文策略 |
| 输出 | codec 的 K/逐码本 vocab/轴/dtype，或 embedding 的 D/dtype/池化和归一化 |

profile 不包含自身 ID、run_id、输入 dataset、机器路径、时间戳或 GPU 编号。
完整 profile 内含 timeline 定义，timeline_profile_id 必须与其 canonical hash 相符。
选择清单、机器/GPU 型号、驱动、实际 batch 配置、耗时、GPU 数量和 worker 数放 run manifest/execution。
不能将影响数值的 batch/padding/backend 差异仅标为性能参数；如未证明批处理不改变每条结果，
应使用逐条基线或把确切处理语义纳入新 profile，不静默混合旧缓存。
worker 数和调度顺序不参与业务 ID；profile 声明 per_target_independent 时必须按以下数值比较规则验收不同 batch 组合。
profile.reproducibility 固定比较器：codec 使用 exact_integer；speaker 使用 elementwise_atol_rtol，
atol/rtol 为已实测验收的有限非负数。容差不能任意放大以通过验收；额外余弦/范数标准也须固定在 profile。

profile_id 只对完整 profile 对象计算，不包含示例包装中的 example_only/runnable/note。
生产 profile 不允许未解决的 null/占位符。无操作明确写 none。仓库中的候选模板 runnable=false，
不以模板 hash 冒充已验收的模型 profile。实际权重与前处理验证完成后才生成生产 ID。

## 3. 输入、时间轴和关联

生成时只读取 complete manifest 指定的 base/view snapshot，不接受转换中的目录。
基础表负责保留音频，特征表只保存结果和指纹。run inputs 固定 manifest SHA256、table_path 和整数 snapshot。

| 公共字段 | 语义 |
| --- | --- |
| target_kind / target_id | sample 或 view；run 内 target_id 唯一 |
| parent_sample_id | sample 时等于 target_id；view 时指向原始音频所在 sample |
| profile_id | 与目录和 manifest 一致 |
| audio_sha256 | 原始编码 audio.bytes 的 SHA256，绝不以路径代替 |
| timeline_profile_id | 本行实际使用的原生解码时间轴 |
| native_sample_rate | 该原生时间轴的 Hz，int32 > 0 |
| start_frame / end_frame | int64 原生采样帧半开区间，绝不是 codec 帧 |
| encoder_input_num_frames | 前处理后送入 encoder/frontend 的有效波形采样帧，排除 batch padding |
| encoder_input_sha256 | 上述有效波形的规范化摘要，不是原始文件摘要 |
| feature_key | 音频内容、时间区间、profile 的可复用计算身份 |
| input_fingerprint | 本次 target 与该计算输入的绑定，公式见下 |
| status / error_code | ok / failed / unsupported / skipped；非 ok 必须说明原因 |

sample 任务先按所选时间轴解析明确区间：首版严格模式用 `[0, base.num_frames)`，
解码必须核对采样率、有限值和有效总帧数。头信息与完整解码不一致则失败，不能默认补零/裁尾。
view 使用已验证的原生坐标和同一 timeline；不能将另一解码器的秒数简单换算冒充同一时间轴。
错误行保留计划中的父引用、音频哈希和区间；无法确定有效区间的对象必须先解决时间轴，不能假装完成编码。

首版推荐顺序：完整解码/长度核验 → 原生坐标裁剪 → 明确声道策略 → 重采样 → encoder/frontend。
完整录音先编码再切 token 不等价于波形先裁剪再编码；不允许由时间比例猜 token 边界。
长音频若超过已验收容量，标 unsupported 或先生成显式 view；不偷偷截前 N 秒。
参考裁剪也成为 view 或被已有 sample 表达，不能把随机裁剪的结果挂在整条 sample 下。

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
不同 run 的同 key 浮点结果可能摘要不同，build 必须固定实际 run/snapshot/摘要；跨 run 复用也须验证来源，不能查 latest。

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

## 5. Qwen3-TTS 首个候选

参考当前训练实现，首个候选固定方向如下，仍须实际模型验收：

| 项目 | 候选 |
| --- | --- |
| codec | Qwen3-TTS-Tokenizer-12Hz；使用冻结 tokenizer，Talker 重新初始化与 codec profile 无关 |
| 波形 | 24,000 Hz 单声道 float32；原生坐标先切片，显式重采样 |
| 输出 | [T,16]，16 个码本逐个值域 [0,2048)，存 int16 |
| 推理 | eval + inference_mode；先 float32 基线，其他精度另行验收/profile |
| 幅度/静音 | 不额外归一化、去静音、裁短或增强 |
| 重采样候选 | scipy.signal.resample_poly，gcd 约分，Kaiser beta=5，constant/cval=0；固定版本与输出长度规则 |

候选采纳上述重采样策略时 N=ceil((end-start)*24000/native_sample_rate)，24k 输入直接保留；
需用实际前处理实现验证此规则。不能在 profile 写 scipy，而实际走 codec.load_audio 的另一重采样器。
Qwen 名称中的 12Hz 不作为通用时间换算常数。模板中的模型 revision 来自已检查的训练仓库，
生产仍核对实际权重/config，不因仓库声明了下载版本就宣称本机已经验证。

## 6. 索引和高吞吐读取

features.lance 必有 target_id、feature_key 的 BTREE；view 特征另建 parent_sample_id BTREE。
parent_sample_id 在固定 base snapshot 的 sample_id 索引上查找；view 还需核对对应 views snapshot。
按 ID 取少数结果无需把所有文件 load 进内存。批量全库生成使用顺序/分片扫描，
投影所需列；构建训练数据时批量关联，可用本地盘外部排序/SQLite，禁止逐行远程随机查询。
索引不保证唯一性和外键，发布时必须显式验证。

默认训练 build materialize 选定 codec/embedding，使每步训练不跨特征表 join。
业务引用仍保留，读取不依赖执行 batch 或临时状态。在线 speaker 路径的音频读取见 11。

## 7. 生成、失败、续跑和验收

1. 固定 inputs/profile/target_kind；all_samples 对快照全体 sample，all_views 对固定 view 表。
   任意子集在推理前将选择写入本 run 的 targets.lance 并冻结 snapshot、schema 与集合摘要；不能只写易变查询字符串。
   选择表与结果表一起原子发布，由 run manifest 绑定，不需要另一份 selection manifest。
2. 按目标 ID 与 input_fingerprint 建有界任务；CPU 解码、GPU 推理、Lance 写入分工。
   每个 GPU 一条受控推理队列，CPU workers 与 GPU batch 是不同参数；不把 128 workers 当 128 个 GPU 进程。
3. GPU worker 只返回已校验结果；单一协调者提交同一张表。并行 worker 写未提交 fragment，
   只有已写回、哈希和读回验收成功的 checkpoint 可恢复。
4. 调度/checkpoint 键使用目标与指纹，不以 worker/rank/物理行号作为身份；保存 code/dependency/profile 摘要。
   改 workers 不重新编码已验证目标；改 profile 新开 run。未提交 orphan 在核对可达性后才清理。
5. 工作目录位于 dataset/.state/<release>/features/<kind>/<profile>/<run>/；
   共享盘只保留小状态和输出。身份唯一性审计 SQLite 放本地 TMPDIR，批量写入、最后建辅助索引，完成删除。
6. 生成到 `<run_id>.incomplete/`，检查 coverage、shape/range、所有输入引用和有效输出 hash、索引、
   固定 snapshot 可读，再原子发布 `<run_id>/`。不修改 samples.lance 或其他已发布 run。

published rows = selection.target_count = coverage.total_targets = ok + failed + unsupported + skipped，missing=0。
complete 表示任务范围完整记账和存储验收完成，不等于全部成功；仅 ok 行可用于该特征的训练。
未尝试/进程中断仍留 incomplete，不伪造 skipped；skipped 必须由固定的显式策略决定。
记录每种 error_code 数量、成功输入时长、失败覆盖与校验范围；不能以“表能打开”作为全部验收。

同一未发布 run 可对失败目标重试，最终只保留一个结果。已发布 run 的补算产生新的自包含 run，
可复用已验证成功值；也可让 build 明确选择 fallback run，并核对相同 profile/指纹。
绝不查询 latest 来补洞，不覆盖旧 snapshot。

首个生产 profile 的验收必须包括：多采样率/声道/长度/语言、空或损坏音频、batch padding/重排、
长音频边界、编码再解码抽检、真实长度与值域、失败记账、断点/换 worker、Lance 按 ID 查询、训练吞吐。
codec 还需听检重建样本和记录有效帧数；不能将 codec 重建保真度当作原始语音质量评分。
本章定义格式和验收条件，不表示 GPU 执行器和训练读取器已实现。

## 8. 选择范围与 targets.lance

selection 固定 mode、input_alias、available_target_rows、target_count、target_set_sha256。
input_alias 指向 inputs 中唯一的目标表：sample 对 samples，view 对 views；view 的父 base 另在 inputs 固定。
available_target_rows 是该目标表指定 snapshot 的总行数，绝不是 view 对应的父音频数。
all_samples/all_views 要求 target_count 等于 available_target_rows，不创建 targets.lance；
subset 要求 `0 < target_count <= available_target_rows`，允许显式列举全体。空选择不发布 feature run。

subset 的 selection.table 必含 table_path（相对 release 根的本 run targets.lance）、lance_version、
schema_sha256、rows；rows 等于 target_count。类型见 schemas 中 feature_targets：
target_kind、target_id、parent_sample_id，均为非 null string。一个 run 只允许一种 target_kind 和一个目标表别名。
sample 的 parent_sample_id 等于 target_id；view 的父引用必须与固定 views/base 快照一致。
发布前全量核验目标唯一、存在、父引用正确，features 的目标集合与选择集合精确相等，不能只比较行数。
targets 建 target_id BTREE；parent 可以重复，不能把多个合法 views 合并成一个任务。

target_set_sha256 对按 (target_kind,target_id) 字符串升序排列的唯一目标做流式 SHA256：
每行是 `canonical_json([target_kind,target_id,parent_sample_id]).encode("utf-8") + b"\n"`。
all 模式也计算同一摘要；它与固定输入 snapshot 共同定义范围，不受扫描顺序影响。
选择表在推理前验证并写入执行计划；恢复必须匹配原摘要和 snapshot。发布后两个表都不可改写，均为保留根。
示例见 [view 子集 manifest](../examples/feature-subset-manifest.example.json)。
