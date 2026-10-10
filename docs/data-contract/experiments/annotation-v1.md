# Annotation 实验契约

状态：实验设计，版本 `exp-annotation-v1`。本稿规定训练质量档案、文本修订和音频恢复的实验边界，
用于实现与小规模验证；不修改已发布数据，不表示模型、阈值或执行器已经验收。
基础 `contract_version=v0.1` 不变，本实验版本不能冒充生产 schema 或已验证 profile。

## 与正式契约的关系

继承 [身份与版本](../specs/03-identity.md)、[annotation 状态与依赖](../specs/05-annotations.md)、
[selection](../specs/12-selections.md) 和 [文本及 merged](../specs/13-text-features.md) 的追溯与发布规则。
正式 05 当前仍以 samples 的 annotation 分支作为密集一对一结果的默认布局；
**本实验使用独立结果表**：一对一任务采用 `storage_kind=sample_table` 单表，一对多采用 `result_table` 双表，不向 samples main 增列。
当前 audio_stats-v2 和 qwen-asr-transcription-v2 均采用单表；历史 transcription-v1 双表保留兼容，已发布版本通过新 run 迁移，不覆盖原版。
实验通过后再协调修订正式规范、机器 schema、示例和执行器；不能只凭本稿切换生产读取方式。

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
- selection 采用固定 annotation 版本，决定训练资格和最终内容；评估集隔离属于明确排除规则。
- 语言、说话人、句长等覆盖属性单独保存。训练比例、权重、预算和 batch 由训练端管理。
- 原始版本和三个独立特征表继续保留；merged 是额外交付，不解除上游审计依赖。

通用文本过滤由 [Selection 文本过滤实验契约](selection-text-filter-v1.md) 定义，
不通过改写 annotation 的状态或结果表达排除；四个游戏的语言一致性策略另行验证。

## 范围和存储

每个任务执行前固定 dataset/release、基础 manifest 摘要、表快照及目标集合。
实验抽样可以使用固定 ID 子集；全量质量档案覆盖固定 base 的目标范围，不能默认只检查旧 merged 的入选样本。
尚未发布或仍在变化的数据源不在运行途中加入，后续使用新的明确输入范围。

沿用正式独立表布局：

```text
annotations/<task>/<run_id>/manifest.json
datasets/<dataset_id>/<release_id>/annotations/<task>/<run_id>/
    results.lance                 # 两种布局均有
    targets.lance                 # 仅 result_table 布局有
```

sample_table 的 results 每样本一行，保存状态、失败原因及可空 result，失败也保留该行。
result_table 则以 targets 按目标完整记账；results 保存成功结果，使用 `(target_kind, target_id, item_id)` 唯一定位。
一对一结果的成功目标对应一项；事件任务可以成功但没有事件，`item_count=0`。
各模块独立写入与发布；worker 通过分片及提交协调器汇总，不要求多个模型并发修改同一张表。
另以 `quality_summary` 任务物化每个目标一项的轻量档案，引用详细证据，不复制音频或所有逐帧输出。

以下字段是实验的逻辑字段约定，Arrow 类型、产物引用结构与具体 schema 摘要在实现试验中冻结。

## 身份和执行状态

每项标注必须能解析到 dataset/release、目标身份、准确的音频对象及其哈希。
同一原始 sample 的恢复音频必须具有独立派生身份；不能只用父 sample_id 区分两种波形。
results 可以通过目标表及 manifest 取得公共元信息，不要求每行重复模型配置和长路径。

| 对象 | 必须固定的信息 |
| --- | --- |
| run manifest | 目标范围、输入 manifest 摘要、table/branch/version、输出固定快照、schema、覆盖统计 |
| task profile | 模型 ID、权重 revision/摘要、推理实现、数值精度、前处理、分块与拼接、指标定义 |
| target | `target_kind`、`target_id`、`input_fingerprint`、`status`、`error_code`、`item_count` |
| 文本相关输入 | 被核验的 `text_revision`、固定文本来源、比较或规范化配置 |
| 局部结果 | `item_id`、实际粒度、对应音频/文本范围、模型真实输出的分数或标签 |

status 沿用 `ok / failed / unsupported / skipped`。未完成不是 skipped；
目标范围外与范围内尚无终态结果分别统计，complete 时范围内 missing 必须为零。
失败不产生假分数，不用零向量、NaN、Inf、-1 代替结果。依赖失败可以明确记为 skipped/upstream_failed，
与之无关的任务继续；某个质量分数低不会自动终止其他计划标注。
普遍性的模型加载、身份或来源冲突应停止相应阶段，不能作为大量坏样本静默跳过。

input_fingerprint 绑定实际依赖：纯音频任务绑定音频对象、区间、时间轴和前处理；
音文任务额外绑定文本版本，派生判断绑定实际使用的上游结果版本。
只换文本可以复用经核验的纯音频结果；换音频不能沿用旧音频的检测结果。

## 时间和文本坐标

音频证据统一映射到对应音频对象实际解码后的原生采样点坐标 `[start_sample, end_sample)`，
满足 `0 <= start_sample < end_sample <= decoded_num_samples`，同时固定采样率和解码时间轴。
模型分帧步长、偏移、重采样映射及边界取整写入 profile，保留真实定位精度；整数采样点不代表采样点级准确。
annotation 的音频区间不是 codec 帧号，不能因某个 speaker reference 使用 codec 边界就共用坐标。

文本差异使用明确字符串上的 Unicode code point 半开区间；词级结果另记录分词单元及其到文本的映射。
规范化前后分别标明坐标所属文本。不具备可靠音频定位的文本差异，其音频范围为空。
整句评分不伪装成字词评分，窗口评分不解释为窗口内每个时刻均有缺陷。

## 标注任务和模型配置

下表的主候选用于首轮验证，不代表全量必须运行所有模型。
最终选用模型、语言路由、精度和阈值必须在试验后冻结；不引用 latest 或依赖隐式默认值。

| task | 逻辑结果 | 实验模型或方法 |
| --- | --- | --- |
| `audio_stats` | 实际时长、采样率、声道、峰值、RMS、近满幅比例、低能量区间 | 确定性波形统计；阈值和声道处理固定 |
| `audio_quality` | DNSMOS 的 SIG/BAK/OVRL、校准前后值、窗口范围、窗口均值/最低值/数量 | Microsoft DNSMOS P.835 非 personalized 版本 |
| `spoken_language` | 音频语言、模型置信输出、分析范围、与来源语言是否一致 | FireRedLID |
| `transcription` | 独立 ASR 文本、识别或指定语言、实际处理范围、分块信息 | Qwen3-ASR-1.7B 多语言基线；FireRedASR2-AED 中文候选 |
| `text_agreement` | 原始/规范化 CER、WER、分母、增删改数量、文本差异范围 | 版本化归一化、分词和编辑距离算法 |
| `text_alignment` | 字词单元、文本范围、音频范围、时间异常 | Qwen3-ForcedAligner-0.6B |
| `speaker_structure` | 样本内 speaker 区间、估计人数、重叠区间及摘要 | NVIDIA Nemotron-3-Diarization 候选，pyannote Community-1 对照 |
| `text_revision` | `action=keep/replace`、候选文本、修订依据 | 消费转写、规则或人工核验；遵守正式 05 的文本修订语义 |
| `restoration` | 执行状态、派生音频引用、输入/输出哈希、采样率/长度及处理谱系 | sarulab-speech/sidon-v0.1 |
| `quality_summary` | 四维判断、原因、证据引用、覆盖属性 | 固定版本的评估规则或经校准的评估器 |

ASR 的补充候选收敛为：Cohere Transcribe 03-2026 对照多语言准确率，Parakeet-TDT-0.6B-v3 对照英语/MLS 吞吐，
Whisper large-v3 作成熟基线；Fun-ASR-Nano 可用于中文/日文对照。无需把这些模型都变成全量依赖。
所有模型只在其明确支持且完成验证的语言上启用。上游转写模型已知时记录来源，避免把同源重复识别称为独立核验。

### 分数解释

- DNSMOS 是预测听感，不是音文正确率。短音频的官方流程会重复自身，若采用必须记录输入延拓方式；
  重复后的窗口不能冒充真实的多个音频区间。补充 MOS 模型先做对照，不直接混合不同模型或来源分数。
- audio_stats 的低能量区间仅表示固定阈值下的波形统计，不能解释为无语音区间。
  音文长度比使用当前选用音频范围的实际完整解码时长，不扣除低能量区间或首尾停顿；
  该比值不解释为有效语音时长或真实语速，是否用于过滤由 selection 的固定规则决定。
- ASR 不使用被核验文本作为提示。来源语言和实际指定给模型的语言分别记录；语言冲突保留证据。
- CER/WER 以被核验文本为 reference，ASR 为 hypothesis；保存替换/删除/插入绝对数量，允许比率大于 1。
  reference 为空时比率为空并注明原因；不适用的 WER 不用 0 占位。比较规范化不直接改写训练文本。
- Qwen 对齐保存时间戳，不构造模型未提供的声学支持分数；对齐成功不能证明原文正确。
  荷兰语、波兰语等不在该对齐器官方支持范围内的输入明确记为 unsupported，不因此直接判样本不合格。
- speaker 标签仅在当前音频对象内部有效，不写回全局 speaker_id；保留重叠，不强制只允许一个 speaker。
  重叠比例为重叠区间并集时长除以实际分析音频范围的完整时长；speaker 人数是估计值。
  未执行或失败时结果为空，不能把没有结果当作零重叠。
- 首尾低能量和对齐异常属于完整性证据，不能单独证明音素截断或异常拼接。

FireRedASR2-AED 的 AED 是 Attention-based Encoder-Decoder，属于转写模型架构。

### 四游戏 spoken_language 首次实现

具体字段和发布规则见 [05 · FireRedLID 音频语言单表](../specs/05-annotations.md#fireredlid-音频语言单表)。
`firered-lid-v1` 独立单表覆盖原神、星铁、鸣潮、Zenless 的固定 samples 快照；
保留来源语言、FireRedLID 原始标签/规范化标签/置信输出，ASR 语言仍在 transcription 中独立保存。
不互相覆盖、不预先按语言过滤、不提示预期语言。置信输出不是校准概率，冲突由 selection 解释。
不做 30 秒切分；按用户确定的规则，最多使用前 200 秒，记录真实原生范围和截断标记。
FP32 与官方单条推理对照后采用分桶批处理；短至不能形成声学特征的输入明确失败，不补波形。

## 文本修订和音频恢复

文本修订沿用正式 05：`ok/keep` 时 text 为空，`ok/replace` 时 text 为有效非空文本，
failed 不等于 keep。selection 显式采用候选修订，ASR 输出不自动替换 base 文本。
采用新文本后重新核验依赖文本的音文指标，并按最终选用文本重新处理相关去重冲突。

restoration 在档案中表现为一个结果项，但音频实体按 [派生音频约定](../specs/02-base-schema.md#音频与派生)
独立保存，不能把波形塞进普通质量分数或覆盖原 sample.audio。
结果引用须固定派生对象身份、产物 manifest/table/version、父音频、变换 profile 和输入输出摘要；
派生音频的物理布局与 selection 消费 schema 待实现试验冻结，本稿不另引入训练 assets/builds。

Sidon 恢复前后分别绑定音频对象，保存或引用音质、ASR/音文一致性、时间关系和必要的音色保持核验。
MOS 上升不自动证明恢复成功；不能把一次转写变化直接解释为原文或恢复音频错误。
模型输入重采样、边界处理、输出实际长度和编码方式明确记录，不默认原生采样点一一对应。
已存在上游恢复处理的来源记录处理谱系，不仅凭 dataset 名称决定重复恢复或跳过。

第一版实验使用单说话人 Sidon；多人分离不是普通 restoration 的隐式功能。
失败保留状态，selection 可以明确采用已核验的原音频，但不能把回退伪装成 restoration 成功。

采用恢复音频后，针对该对象重新生成 codec、speaker embedding、alignment 和相关质量结果。
随机 reference 的区间及其到完整 codec 的映射也必须重新建立与验证，不沿用旧波形的定位。
文本内容可经核验保留，但文本特征和 merged 必须绑定新的正确音频身份；旧产物继续保留。

## 质量汇总与筛选

质量汇总按声学质量、音文配对、语音纯度、片段完整性分别输出：

```text
verdict: pass / fail / uncertain
reason_codes
evidence_refs
assessment_profile_id
```

只有已执行且适用的证据可以支撑判断。证据缺失或冲突保留 uncertain，
即使汇总程序成功执行，也不能把所有维度默认置为 pass。模型置信度不命名为经校准的正确概率。
首版不生成综合 quality_score，不把多个分数相乘，不提前写死 MOS/CER 的入选阈值。

覆盖属性保存来源、语言、speaker 来源、句长、实际完整音频时长、音文长度比、数字/混语等可验证属性及已有重复关系引用；
不为齐全而默认新增专名识别或全局声纹聚类。口音、低沉嗓音、情绪、长句、快速语音不天然扣分。
去重最终裁决和资格规则归 selection；语言比例与采样权重归训练端。

## 验证与发布

先固定跨数据集、语言、时长和已知问题的试验集，人工核验量按问题覆盖和误判情况调整。
评估使用人工核对的文本或判断，不能直接把当前 base 文本当作绝对真值。
除识别 CER/WER 外，重点检查漏词/多词、专名、误删正确样本、缺陷漏检和来源偏差。
不同模型的相同错误不能作为独立确认，人工验证保留按原录音来源隔离的样本。

全量前必须验证：

1. 输入身份、文本版本和各时间轴可追溯，恢复音频与原音频不会错配。
2. 重跑、失败重试和不同 worker 数不改变目标身份；有随机性的处理固定种子及策略。
3. 满载分桶/批处理与单条基线对照，模型原生边界处理与 mask 经验证，不引入跨样本污染。
4. 统计按语言和来源的成功率、unsupported/failed/skipped、质量分布及人工误判。
5. 测量包含 Lance 读取、解码、推理和写回的吞吐、存储、显存及预计总耗时。
6. 发布前全部计划目标终态记账、结果唯一性和引用校验通过，固定快照再发布 complete manifest。

实验产物保存在 artifacts，显式标明实验用途，生产 selection 不自动消费；
正式运行使用上述发布布局且显式绑定通过验证的 profile。每次发布保留输入及其传递依赖。

## 当前实现边界

- 已有 annotation schema 帮助函数和正式实验示例，不等于通用 annotation 执行器已实现。
- 独立 Qwen ASR transcription 和 CPU audio_stats 已有专用执行器与任务 schema，按正式 05 发布；其他质量模块仍待实现与验证。
- 当前 selection 执行器是 first-root 路径，拒绝生产 annotation 输入；需补齐后续 selection 的排除继承与消费。
- 当前 text 执行器尚不支持采用 annotation 原始修订文本并完成 revision 校验。
- 当前 merge 会要求 codec 内的九个兼容文本元数据列与独立 text 完全相同。采用修订文本后，
  即使音频与 codec 数组可复用，也需要新版本的元数据绑定及 merge 验收；不能直接用新 text 拼旧 codec 表。
- 恢复音频的派生发布、selection 音频选择和下游特征绑定尚待实现。
- 本稿不改变正式 05 的分支默认方式，不替换现有机器 schema 或示例，也不自动同步生产运行配置。

## 模型资料

模型名称描述实验候选；实际运行仍需锁定权重摘要与代码版本。

- [DNSMOS 官方实现](https://github.com/microsoft/DNS-Challenge/blob/master/DNSMOS/dnsmos_local.py)
- [FireRedASR2 与 LID](https://github.com/FireRedTeam/FireRedASR2S)
- [Qwen3 ASR 与 ForcedAligner](https://github.com/QwenLM/Qwen3-ASR)
- [Nemotron Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization)
- [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [Cohere Transcribe](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026)
- [Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)、[Whisper](https://github.com/openai/whisper)、[Fun-ASR](https://github.com/QwenAudio/Fun-ASR)
- [Sidon](https://github.com/sarulab-speech/Sidon) 与 [候选权重](https://huggingface.co/sarulab-speech/sidon-v0.1)
