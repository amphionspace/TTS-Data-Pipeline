# 04 · 视图、对话与时间轴

## 基础对象与视图

基础一行代表一个独立编码音频载体；短句、长录音、对话都可以作为载体。
同一载体的多个训练片段使用 view，不复制原音频。上游已经提供独立剪辑文件时保留这些来源实体，
用 recording/group 或重复标注关联，不能未经波形核对就假定可替换为长录音切片。

视图保存在 `views/<run_id>/views.lance`，每行固定以下字段，类型见 schemas：

| 字段组 | 字段与要求 |
| --- | --- |
| 身份 | view_id、view_revision、view_run_id、source_view_key、view_kind |
| 父引用 | parent_sample_id、parent_audio_sha256，均必填并验证存在 |
| 坐标 | timeline_profile_id、start_frame、end_frame；int64 半开区间 |
| 内容 | text/text_kind、language、speaker_id/speaker_scope；未知为 null |
| 关系 | recording_id、group_id、parent_view_id；父 view 可空且不得形成环 |
| 其他 | metadata_json；保留来源秒数、角色标签、对齐说明 |

一个 view 对应一个连续区间。非连续拼接需新的派生音频及变换记录，不能伪装成单一区间。
视图文本纠错发布新视图 run 或指向该 view 的标注 run，不静默覆盖旧表。

是否使用完整 sample、生成 view 或保留待核实，由
[12 的音频区间决策](12-selections.md#音频区间决策与新数据集接入)在选择阶段明确。
上游的原录音时间范围若已经应用到当前独立片段，不得重复裁剪。
有 parent_view_id 时，start_frame/end_frame 仍指根父 sample 的原生时间轴，不改为父 view 的局部坐标。

## 时间轴

frame 指每声道采样帧，区间为 `[start_frame,end_frame)`；0 ≤ start < end ≤ 指定时间轴有效帧数。
时间轴 profile 固定 decoder 与版本、容器解释、采样率/声道、延迟、padding、有效区间和舍入策略。
所有秒数只用于展示或来源保留；训练裁剪使用整数帧，重采样前后坐标不得混用。
基础 num_frames 是指定读取器对原文件报告的帧数；它不自动证明所有编码都已校正有效时长。
AAC/M4A 等需要专门验收 edit list、编码延迟与尾部 padding 后接入。

Codec profile 指定先裁剪再重采样或相反的实际顺序。长音频整段编码后切 token 与先切 waveform 再编码
一般不能直接视为等价；必须验证边界和上下文影响，且作为不同 profile 表达。

## Speaker、混语与关系

单段确定单 speaker 才填单 speaker_id；多人可为空，轮次/区间 speaker 进入视图或 diarization 事件表。
本地 S1/S2 限定录音/会话；游戏角色限定语言与来源；跨数据集同人须经过单独映射 run。
顶层 language 为空不代表无用，可用分段语言标注得到可训练 view。方言原值在 metadata 保留。

book/chapter/recording/group 用于评估隔离和参考配对候选；group_id 相同不证明同 speaker。
多个重叠视图、同话语多麦克风、同音频不同编码分别记录关系，训练配方显式去重或加权。
不得把所有视图时长直接相加宣称独立语音小时数。
