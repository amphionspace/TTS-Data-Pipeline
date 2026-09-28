# 06 · Codec 与其他特征

## 组织

大特征默认独立于主音频表：
`features/<kind>/<profile_id>/<run_id>/features.lance`，kind 如 codec、speaker_embedding、text_tokens。
同一音频允许多个 profile 与 run。特征生成不复制基础音频，不在每条样本旁创建 NPZ 小文件。
一个 codec run 只有一种固定码本结构和 token 空间；新 tokenizer 权重即新 profile。

## Profile 必须完整

codec profile 固定架构/variant、全部权重摘要、实现 revision、依赖、decoder/timeline、声道策略、
裁剪与重采样顺序/算法/参数、响度/静音/归一化、分块重叠与拼接、推理精度/随机性、码本数、
逐码本 vocab size、输出整数 dtype、轴顺序与 padding/截断语义。未确定项不能用 null 创建生产 profile。
无处理应显式 none；本机文件路径不是模型权重身份。profile_id 按完整内容规范哈希计算。

Qwen3-TTS 只确定架构方向，不在通用 schema 写死 [T,16] 或 vocab=2048。
实际采用的 tokenizer 参数从权重/config 核实并通过编码、重建和数值检查后进入 profile。

## Codec 行

| 字段 | 类型与约束 |
| --- | --- |
| target_kind / target_id | sample 或 view 及其业务 ID；run 内联合唯一 |
| feature_key / input_fingerprint | 计算缓存身份 / 输入依赖指纹 |
| profile_id | 与 manifest 一致 |
| audio_sha256 / timeline_profile_id | 原音频与解码时间轴 |
| start_frame / end_frame | int64，原生时间轴上的实际输入区间 |
| status / error_code | 状态；失败不能冒充空 codes 成功 |
| num_codec_frames | int64，成功时 >0 |
| num_codebooks | int32，与 profile 一致 |
| codes | list<fixed_size_list<integer,K>>；外层 time，内层 codebook |
| codes_sha256 | 成功时必填，固定序列化内容摘要 |

成功行 codes 长度为 num_codec_frames，每帧长度为 K；值分别满足对应码本范围。
失败行 codes、codes_sha256、num_codec_frames 为空。整条 sample 也写完整有效区间，不用 null 隐含“全部”。
支持 int16/int32，具体 profile 固定一种，并保证 vocab/特殊值可表达。磁盘 codes 不加入 batch padding。
codes_sha256 = SHA256(canonical_json({dtype,shape,axes}) 的 UTF-8 bytes + 单个换行 +
按该 dtype 小端 C 连续序列化的原 codes bytes)。

为 target_id 和 feature_key 建标量索引；相同 feature_key 可对应多个来源 target，是否在物理层进一步去重
是可选优化，不能丢掉目标映射。一条 sample 或 view 的特征可以按 ID 随机取，但训练不应每步做跨库散乱 join。

## 生成、续跑与训练选择

任务按固定输入 snapshot 和 profile 并行生成，检查点按目标与指纹恢复；发布前检查缺失、失败、唯一性与形状。
选择性重试发布新的完整 run，或由 build 显式固定 fallback run，不能用 latest 隐式补洞。
纯音频 codec 不因文本纠错失效；音频、区间、decoder、重采样、权重改变均会失效。

训练 build 选择一个模型协议兼容的 codec token 空间；允许混合多个空间时必须由模型协议明确区分。
文本 tokenizer、speaker encoder 各自独立 profile；codec profile 不代替它们。
GPU 生成与模型训练使用独立锁定环境，基础转换环境不需要 Torch/CUDA。
