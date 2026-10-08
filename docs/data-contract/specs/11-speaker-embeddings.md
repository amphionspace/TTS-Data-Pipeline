# 11 · Speaker embedding 与未来解冻

三表独立提取及身份关联见 [13 独立文本表](13-text-features.md)。训练专用构建由训练仓库管理。

## 两条路径，共用原始音频

当前 speaker encoder 冻结；未来可能解冻。保留这两条明确训练路径：

| speaker_conditioning_mode | 训练输入 | 约束 |
| --- | --- | --- |
| frozen_embedding | 所选参考片段的离线 embedding | encoder 与前处理固定；训练不再跑 speaker encoder |
| online_speaker_encoder | 所选参考 sample 的音频引用与原生区间 | 训练时按当前 frontend 生成所需输入；encoder 可冻结或更新 |

统一格式**不要求持久化 mel**。原始编码音频保留在 samples.lance，足以重新提取不同 frontend 的输入。
mel 的采样率、窗、hop、频带、幅度/对数、padding 等都可能变化，不能设成通用共享的真值列。
在线路径必须在 recipe 固定 speaker_frontend_profile；若未来为吞吐缓存 mel/其他 frontend，
它是可重建且按完整 profile 区分的缓存，需要独立设计验收，本次不建立 mel 表或默认缓存协议。

解冻时换用 online_speaker_encoder 和新的训练配置。旧 embedding 仍属于旧 encoder 快照，
可继续服务使用该冻结模型的旧实验，不能用于代替解冻后逐步变化的 encoder 输出。
不需要重做 base，也不因 speaker encoder 更新而重算独立 codec。
encoder 若更新，梯度必须从训练 loss 经过它；离线 embedding 会切断这条梯度路径。

## 特征属于片段，不属于说话人标签

每行 embedding 的 target 是一个 sample。它表达该音频区间在指定 encoder 下的输出，
不是 speaker_id 的唯一属性，也不是 speaker 身份真实性的证明。
同一 speaker 可以有任意多条参考 embedding；不能先求每人平均向量再当作所有片段的标准结果。
说话人聚合、多个参考加权或拼接属于明确的训练/派生策略，不能覆盖逐片段结果。

codec 和 embedding 分别生成与发布，不要求同时完成。一轮任务可以复用解码的原生波形，
但两套前处理不同就必须分别处理，并记录各自 encoder_input_sha256。
参考 embedding 的生成不依赖文本、目标文本 tokenizer、语言采样权重或 Talker 初始化。

## Profile 必须固定的 speaker 语义

除 [06 公共 profile](06-codecs.md) 外，speaker profile 明确：

- encoder 架构、config、实际 speaker 权重与全部必要 buffers、代码和依赖；不能只写 Qwen speaker。
- waveform frontend 的完整算法、所有 mel/其他输入参数和函数摘要；不只固定 encoder 的神经网络权重。
- 输入区间、时间轴、声道/重采样/幅度处理；默认整条已声明的 sample，不在 encoder 内暗中随机裁剪。
- pooling、padding/mask 语义、最短/最长支持输入、eval 模式和精度。
- embedding_dim=D、storage_dtype=float32、normalization=none 或明确的算法/epsilon。
- 输出层：encoder 最终输出、进入 Talker 条件注入之前；不包含 text_pad、训练 projection 或拼接结果。

当前提取的是训练仓库使用的冻结 Qwen ECAPA-TDNN 输出。它不使用 FA2 attention。
生产路径只拼接真实 mel 帧，并通过逐样本 offsets 隔离卷积边界、SE 统计和 attentive pooling；
不添加 waveform/mel batch padding，不裁剪、不分段。原生单条或严格等长推理保留为独立数值参考。
原生 mel 前处理和 encoder 卷积的 reflection padding 保留，它们属于模型算法，不是 batch 补齐。
D 从实际 speaker_encoder_config.enc_dim 核实；当前缓存模型为 1024 维，不写死成 codec K、codebook size 或某一 Talker 的 hidden size。若训练有额外投影，属于模型协议。

以下描述整条音频模式；reference 模式的裁剪和解码差异见本页末节。

FP32 波形前处理遵循训练语义：完整原生解码、float32 声道均值、
默认 sinc_interp_hann 重采样到 24 kHz。缓存的 `Resample(dtype=torch.float32)` 与原生 functional 内核逐值一致。
解码通过内存文件避免 Python 回调争用；仅 Opus 使用经过波形摘要对照的系统 libsndfile，
其余格式保留原有库，profile 固定对应库摘要。mel 在 GPU 上按原生窗、反射边界和公式计算。
不能沿用 codec 的 scipy 重采样替代它。torchaudio 内部以 FP32 计算输出帧数并向上取整，
少数输入的结果与精确整数比例公式相差一帧；必须保留原生输出，分组预测遵循同一长度计算，
不能通过补齐或截断“修正”它。实际 waveform/mel 长度决定 offsets，头信息只用于调度。
卷积使用补偿 TF32x3 的 FP32 路径，最终投影保持原生 FP32；普通 Torch/cuDNN TF32 和 autocast 关闭。
GPU FFT、矩阵乘法和分段归约的运算顺序与原生单条不同，不承诺逐位等同；
必须满足既定逐元素 `atol=1e-5, rtol=1e-4`，写盘摘要仍严格逐字节校验。
原生 encoder 至少需要 5 个 mel 帧（该 frontend 下为 1280 个 24 kHz 波形帧）；
不足时记录失败，不延长音频。帧数以实际解码结果为准，base/容器头的帧数仅用于调度预估，不因帧数差异丢弃样本，也不据此补齐或截断波形；采样率、声道仍须一致。成功行的 `end_frame` 使用实际解码的原生帧数；前处理后的实际输入长度和摘要写入 `encoder_input_*`，分组以实际 mel 形状为准。计算与存储均为 FP32，关闭 autocast、普通 TF32，权重由缓存 checkpoint 的原始值转换为 FP32。
严格等长 batch 与逐条推理仍可能因底层算子计算顺序产生微小数值差异，
按 profile 的 `atol=1e-5, rtol=1e-4` 对照同权重原生逐条 FP32 验收；存储回读与摘要检查仍必须精确。
默认保存 encoder 原始输出，不擅自 L2 normalize；模型原本输出是否归一化亦需核对实现。

若 speaker 权重取自完整 TTS checkpoint，建议导出独立 speaker artifact，固定 tensor names、shape、dtype、
bytes 和 config。来源 checkpoint/revision 记为证据；特征身份不应因无关 Talker 权重重新初始化而改变。
如果 encoder 或 frontend 权重变了，即使仍冻结，也必须新 profile 和新 embedding 结果。

## 行结构与一致性

公共字段及身份公式完全沿用 06，包括 parent_sample_id、音频/波形摘要、原生时间坐标、status 等。
只有公共含义相同，不要求 codec 和 embedding 的 feature_key 或输入波形摘要相同。

| 额外字段 | 类型与语义 |
| --- | --- |
| embedding_dim | int32，所有行均为 profile 的 D |
| embedding | fixed_size_list<float32,D>；成功时 D 个有限值 |
| embedding_sha256 | 对 shape=[D]、axes=[embedding]、dtype=float32 的小端规范数组摘要 |

成功时必须检查 D、全部有限、输入有效、模型定义的输出范围/范数约束；不能把全零向量作为失败替代。
不统一要求单位范数；是否允许零范数由已验收 profile 明确规定，当前候选拒绝零范数。
失败行 embedding 与 embedding_sha256 为 null，error_code 非空；encoder_input_* 的可空规则同 06。
同 profile/feature_key 结果复用时验证 embedding_sha256，不按 speaker_id 复用。
索引、快照、恢复与覆盖数遵循 06，默认不建 ANN 向量索引：这里做精确 ID 读取，并非相似度检索。

## 下游消费边界

离线 embedding 消费者固定特征 manifest/profile 和表快照；在线消费者固定样本及音频摘要。
关联使用样本身份，不使用隐含相同行序；快照内行定位符不是永久 ID，换快照须重新核对。
参考选择、speaker-only/ICL 协议、聚合方式、训练记录字段和条件校验由训练端管理。
跨样本的同 speaker 配对需要确认身份，不能仅凭同 dataset/group 推断；
使用目标自身音频不需要 speaker 标签，但不代表独立参考克隆评估。

## 验收

冻结路径必须对比同一权重/前处理/输入下的在线 encoder 输出，使用 profile.reproducibility 固定的比较器和浮点容差；磁盘摘要验证仍是逐字节。
同 key 的权威结果选择、重算和冲突处理遵循 06；不能用容差结果替换已提交数组。
同一成功 cache 行精确复用不等于跨 GPU 浮点天然完全确定；实际实现/精度和验收环境必须记录。
测试单条/不同长度混 batch、padding/裁剪、参考更换、文本修订不失效、权重更换必失效、
失败不补零、Lance float32 向量与 null 读回。小模型训练需要验证缓存 embedding 接口的真实 loss/梯度行为。
在线路径需验证参考音频定位、frontend 一致性、encoder 参数确实更新以及训练读取吞吐。
规范只定义两条接口；尚未实现的训练分支、真实权重核验和性能问题记录在 pipeline 仓库。

## Codec 帧定位的 reference speaker 版本

`qwen3-ecapa-24k-fp32-reference-v1` 是新增的离线片段模式，不改变整条 speaker 的历史版本。
它固定 selection/base、完整 codec 快照、模型、seed 与 `codec-grid-reference-v1` 策略。
每个 sample 只生成一个 reference，完整 text 和 codec 不裁剪。不是音频 selection 的重新切样本，
不创建新 sample_id，也不使用父录音时间戳。

当前 codec 的时间网格是 24 kHz / 1920 samples，即每帧 80 ms。
先计算所有合法帧长度，再均匀选择长度、均匀选择合法起点。长度须满足实际原音频的
10%～50% 且至少 0.5 秒；没有额外绝对上限。只使用完整时间帧，尾部不完整帧不选入 reference，
但保留在完整 codec 中。最短可选 7 帧（0.56 秒），通常至少 1.12 秒的原音频才能满足约束。
边界换算到原始采样点后再次满足约束；不截短、不补零。

随机身份包含 dataset_id、release_id、sample_id、audio_sha256、固定 seed 和策略版本。
算法为规范 JSON SHA256 派生种子、SHA256 计数器及拒绝采样，避免取模偏差；长度先均匀，再选起点。
采样计划按分片先持久化为 Arrow IPC，重试比对计划，checkpoint 固定其摘要；worker 顺序不影响结果。

所有区间均为左闭右开。speaker `start_frame/end_frame` 是原始采样率下的裁剪点；
映射规则为 `native_boundary(k) = ceil(k * 1920 * native_rate / 24000)`。
新增 nullable 字段（成功行必须有值）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| reference_codec_start | int64 | 完整 codec 序列起始帧，包含 |
| reference_codec_end | int64 | 完整 codec 序列结束帧，不包含 |
| reference_codec_feature_key | string | 对应完整 codec 行的 feature_key |
| reference_native_total_frames | int64 | 对应完整 codec 实际解码的原生总采样点数 |

为与已发布 codec 的时间轴一致，本模式使用其 packaged soundfile 原生解码约定，核对实际长度后，
先裁原始波形，再沿用 FP32 声道均值、torchaudio 重采样及无 batch padding 的 speaker 路径。
输入长度/摘要描述裁后重采样波形，不能与完整 codec 输入摘要直接比较。
模型原生边界处理保留；不同样本不共享卷积、池化或重采样上下文。
坐标描述监督时间网格，不表示 codec token 的感受野仅限于该时间段。

`no_legal_reference`、解码失败、无效 embedding 等保留失败行及原因，embedding 为 null；
不写零向量、不回退到完整音频 embedding。来源冲突或普遍性故障停止阶段，不能静默大量跳过。
该表仍覆盖完整输入 selection；后续 reference merged 仅保留成功行，并报告排除原因。
不生成文本 token、训练 batch 或 loss mask；训练端自行将帧区间映射至预测目标位置。
