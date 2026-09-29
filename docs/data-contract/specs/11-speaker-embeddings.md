# 11 · Speaker embedding 与未来解冻

## 两条路径，共用原始音频

当前 speaker encoder 冻结；未来可能解冻。保留这两条明确训练路径：

| speaker_conditioning_mode | 训练输入 | 约束 |
| --- | --- | --- |
| frozen_embedding | 所选参考片段的离线 embedding | encoder 与前处理固定；训练不再跑 speaker encoder |
| online_speaker_encoder | 所选参考 sample/view 的音频引用与原生区间 | 训练时按当前 frontend 生成所需输入；encoder 可冻结或更新 |

统一格式**不要求持久化 mel**。原始编码音频保留在 samples.lance，足以重新提取不同 frontend 的输入。
mel 的采样率、窗、hop、频带、幅度/对数、padding 等都可能变化，不能设成通用共享的真值列。
在线路径必须在 recipe 固定 speaker_frontend_profile；若未来为吞吐缓存 mel/其他 frontend，
它是可重建且按完整 profile 区分的缓存，需要独立设计验收，本次不建立 mel 表或默认缓存协议。

解冻时换用 online_speaker_encoder 和新的训练 recipe/build。旧 embedding 仍属于旧 encoder 快照，
可继续服务使用该冻结模型的旧实验，不能用于代替解冻后逐步变化的 encoder 输出。
不需要重做 base，也不因 speaker encoder 更新而重算独立 codec。
encoder 若更新，梯度必须从训练 loss 经过它；离线 embedding 会切断这条梯度路径。

## 特征属于片段，不属于说话人标签

每行 embedding 的 target 是一个 sample 或 view。它表达该音频区间在指定 encoder 下的输出，
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
- 输入区间、时间轴、声道/重采样/幅度处理；默认整条已声明的 sample/view，不在 encoder 内暗中随机裁剪。
- pooling、padding/mask 语义、最短/最长支持输入、eval 模式和精度。
- embedding_dim=D、storage_dtype=float32、normalization=none 或明确的算法/epsilon。
- 输出层：encoder 最终输出、进入 Talker 条件注入之前；不包含 text_pad、训练 projection 或拼接结果。

当前候选为训练仓库使用的冻结 Qwen ECAPA-TDNN 输出。D 从实际 speaker_encoder_config.enc_dim 核实，
不写死成 codec K、codebook size 或某一 Talker 的 hidden size。若训练有额外投影，属于模型协议。
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

## 训练如何绑定参考

训练 recipe 固定 reference_policy 和 speaker_conditioning_mode，两种模式使用相同的参考身份和区间。
每条训练记录保留 target、ordered_reference_ids、对应父 sample，以及实际使用的参考特征 profile/feature_key/摘要，
以及所有被引用表的 snapshot（表级统一字典即可，不必逐行重复完整 manifest）。

- speaker-only 条件：目标 codec + 冻结参考 embedding 或在线参考音频；不要求参考 codec 或参考文本。
- ICL/prompt 条件：目标 codec + 所需参考 codec/文本，按模型协议决定是否还需要 embedding。
- 在线 encoder：目标 codec + 参考音频引用；embedding 在 forward 中产生，不读取旧 embedding 代替。

默认选有证据属于同一 speaker 的另一条不重叠片段，排除重复编码和评估泄漏。
如果实验要用目标自身作 speaker 条件，必须显式 reference_policy=self；不能伪称为独立参考训练。
未确认 speaker 的样本不能靠同 dataset/group 配对；按 recipe 排除或进入明确支持的无 speaker 任务。
多个参考是否聚合、如何聚合、是否用文本由模型协议固定，不强行在基础 embedding 表求平均。

frozen_embedding 下默认 materialized_codes 布局同时内嵌选定 reference_embeddings 和实际维度，
仍保存 reference IDs 和特征摘要，训练每步不再查独立 embedding 表。
online_speaker_encoder 下 build 记录固定 base snapshot + sample_id + view/区间；
按批量解析并使用有界本地缓存，缓存键至少覆盖音频哈希/时间轴/区间/前处理 profile。
可保存 snapshot 内 row locator 加速，但它不是永久 ID，换快照必须重建；不做每条音频的全表扫描。
训练不需要把整个 base 表或音频列预先 load 到主进程。

### 条件字段矩阵

model_protocol 固定 conditioning=speaker_only/icl、uses_speaker_conditioning、requires_reference_text。
require_reference_codec 必须等于 conditioning==icl，不能作为与协议矛盾的独立开关。
speaker_only 必须使用 speaker 条件，且 requires_reference_text=false。ICL 可显式启用或关闭 speaker 条件。

| 条件 | 必填 | 不启用的字段 |
| --- | --- | --- |
| frozen_embedding | speaker_feature_profile_id；每个参考的 reference_embeddings | speaker_frontend_profile=null；reference_audio 空；speaker_encoder_trainable=false |
| online_speaker_encoder | speaker_frontend_profile 完整定义；每个参考的 reference_audio | speaker_feature_profile_id=null；reference_embeddings 空；trainable 可 true/false |
| ICL 不使用 speaker | reference_codes；协议要求时 reference_texts | speaker_conditioning_mode=null；两个 speaker profile=null；trainable=false；embedding/audio 空 |
| speaker_only | 上面两种 speaker 路径之一 | reference_codes、reference_texts 空 |
| icl | 每个参考的 reference_codes；requires_reference_text=true 时每个参考的 reference_texts | requires_reference_text=false 时 reference_texts 空 |

空表示缺省、null 或 []；启用的记录字段是按 ordered_reference_ids 排列、长度等于 reference_count 的非 null 元素列表。
共同身份列表含 kind/id/parent_sample_id 与输入快照别名，不能仅凭数组位置推测来自哪段音频。
reference_embeddings/reference_codes 的每项含所需特征定位及 profile/key/payload 摘要；materialized 布局还含数组。
reference_audio 的每项含固定 base/view 引用、音频哈希、timeline、原生 start/end 与采样率；不要求离线特征 key。
reference_texts 的每项绑定对应参考和所选文本修订/tokenizer；多参考聚合规则必须由模型协议声明。

other_same_speaker 要求 require_confirmed_same_speaker=true、allow_same_segment=false、allow_time_overlap=false；
self 要求 reference_count=1、allow_same_segment=true、allow_time_overlap=true，参考身份与目标一致。
仅改 policy 而遗留矛盾开关是非法 recipe。运行时仍需实际核验身份/重复关系/重叠，不能只看布尔声明。
模式示例见 [training modes](../examples/training-modes.example.json)；示例只展示条件字段，不是完整生产记录。

## 验收

冻结路径必须对比同一权重/前处理/输入下的在线 encoder 输出，使用 profile.reproducibility 固定的比较器和浮点容差；磁盘摘要验证仍是逐字节。
同 key 的权威结果选择、重算和冲突处理遵循 06；不能用容差结果替换已提交数组。
同一成功 cache 行精确复用不等于跨 GPU 浮点天然完全确定；实际实现/精度和验收环境必须记录。
测试单条/不同长度混 batch、padding/裁剪、参考更换、文本修订不失效、权重更换必失效、
失败不补零、Lance float32 向量与 null 读回。小模型训练需要验证缓存 embedding 接口的真实 loss/梯度行为。
在线路径需验证参考音频定位、frontend 一致性、encoder 参数确实更新以及训练读取吞吐。
规范只定义两条接口；尚未实现的训练分支、真实权重核验和性能问题记录在 pipeline 仓库。
