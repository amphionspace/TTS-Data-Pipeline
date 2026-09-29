# 02 · 基础表字段

基础主表 samples.lance 的 27 列使用 Arrow schema；schema_version=v0.1。
audio 为 struct<bytes:large_binary,path:string>，只选择标量列时无需读取音频。
类型描述见 schemas/arrow-schemas.json，基础字段校验独立于 selection 分支的派生列。

| 字段 | 类型 | 语义与空值规则 |
| --- | --- | --- |
| schema_version | string | 必填，当前序列化为 v0.1 |
| sample_id | string | 必填，来源对象身份，算法见身份约定 |
| record_revision | string | 必填，记录内容修订哈希 |
| dataset_id | string | 必填，稳定的来源数据集名称 |
| source_snapshot | string | 必填，不可变来源版本，独立于任务分组 |
| source_key | string | 必填，来源版本范围内唯一的对象键 |
| source_config | string | adapter 选定的来源配置，可空；合并配置可用 all，原值保留在 metadata_json |
| source_split | string | 当前统一输出固定为 train；原来源划分保存在 metadata_json |
| source_locator_json | string | 必填，编码为 JSON object 的来源定位信息 |
| recording_id | string | 可空，原始录音身份，需包含来源命名空间 |
| group_id | string | 可空，相关音频/章节/会话的分组；不等同于 speaker |
| parent_sample_id | string | 可空；派生音频引用父对象 |
| audio_sha256 | string | 必填，原编码 bytes 的 SHA-256 |
| text | string | 可空，明确选定的一份基础文本 |
| text_kind | string | 与 text 同时存在或同时为空，说明 original/normalized/asr 等来源语义 |
| language | string | 可空，已知语言用统一语言标签；混语按视图或分段表达 |
| speaker_id | string | 可空，来源范围内说话人键 |
| speaker_scope | string | 与 speaker_id 同时存在或同时为空，定义该键有效的范围 |
| metadata_json | string | 必填，JSON object；没有补充信息时为 {} 的字符串 |
| audio | struct<bytes:large_binary,path:string> | 必填，bytes 非空；path 为显示文件名，不依赖机器绝对路径 |
| sample_rate | int32 | 必填，音频对象原生采样率，正整数 |
| channels | int16 | 必填，原生声道数，正整数 |
| num_frames | int64 | 必填，指定读取器报告的原文件采样帧数；不自动等于去除延迟/padding 后的有效帧数 |
| duration_seconds | float64 | 必填，num_frames / sample_rate，有限正数 |
| segment_start_frame | int64 | 可空，派生音频在父对象时间轴上的起点 |
| segment_end_frame | int64 | 可空，与 start 同时存在，半开区间终点 |
| text_variants | list<struct> | 必填，无其他文本时 []；元素为 kind/text/language 三个 string 字段 |

## 文本、语言和说话人

不将空串或 unknown 写入表示未知的字段；转为 null，并在必要时保留原始值。
text_variants 的每个元素应有非空 kind 和 text，language 可以为空。基础文本及变体保留来源文本，
后续人工纠错、ASR、标准化、翻译和音素结果进入版本化标注。翻译不能冒充音频的原语言转写。
含游戏模板、角色分支、标点或特殊控制标记的文本先保留，训练使用的清洗结果另有版本。

语言标签在 adapter 映射表中固定，例如 English(US) → en-US；无法确定的方言不凭猜测映射。
原始 language 字符串保留在 metadata_json。源数据的角色名字不是已验证的真实音色身份。
不同语言配音、不同录音中的局部 speaker_0 不合并。跨来源同人映射作为后续标注。

## 来源与元数据

source_split 表示统一后的输出划分，固定为 train；Lance 本身没有 HF split 接口。
metadata_json 保留 original_split、原始配置、原始标签及未标准化字段。
源无划分时 original_split 留空或不提供，不能因输出为 train 而伪造上游官方划分。
source_locator_json 使用 root 别名与相对路径，可附成员名、行号；根目录搬迁不改身份。

通用连接键使用结构化列；metadata_json 用于保留上游差异，不承担训练期间大规模 JSON join。
JSON 使用固定编码，不允许 NaN/Infinity；缺测指标保留 null。来源分数的原始值可以保留，
用于训练的正式分数进入有指标定义的版本化标注列或结果表。

## 音频与派生

基础接入保留原编码、采样率、声道，不为了统一字段而重采样或统一有损编码。
v0.1 基础 num_frames 取 SoundFile/libsndfile 报告值，具体依赖记录在 manifest；standard 检查头信息，deep 核对完整解码帧数。
生成 view/codec 时另行固定并验证 timeline profile，不把基础头信息自动当成已经验证的有效时间轴。
必须区分容器时长、解码长度和有效长度，不能混用同一个 num_frames 语义。
派生裁剪记录的 num_frames 描述新音频；segment 坐标描述父音频。若重采样，两者不必数值相等。
父对象的版本、时间轴、处理 profile 在派生元信息中固定，父子引用及区间边界必须验收。

## 追加列与基础验证

基础 ingest 必须恰好具有这 27 列；selection 分支可增加 selection_reason/selection_flags，不能修改这些基础字段。
验证基础记录时显式投影这 27 列，计算 record_revision 不包含后加列。
生成新音频（裁剪/重编码/去噪）需要新的派生对象及变换身份，不能直接修改原 sample 的 audio。
v0.1 原始接入中 parent_sample_id、segment_start_frame、segment_end_frame 通常为空；
启用派生音频写入前必须验证父引用和坐标，不能仅填三个字段就宣称关系成立。
