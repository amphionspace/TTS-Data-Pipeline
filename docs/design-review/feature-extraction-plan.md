# Codec / speaker：当前决定与实施门槛

状态：方案与接口准备，未启动真实特征提取。这轮只验证 `/tmp` 的 Lance selection/绑定机制，
没有修改生产 samples、建立生产分支或改动 LM-TTS-Training。
规范源为 [06](../data-contract/specs/06-codecs.md)、[07](../data-contract/specs/07-training-builds.md)、
[11](../data-contract/specs/11-speaker-embeddings.md)、[12](../data-contract/specs/12-selections.md)。

## 已确认的选择

- 首版从头训练 Qwen3-TTS 结构，使用冻结的官方 tokenizer；speaker 条件采用目标自身音频（self）。
  每个入选目标需要自己的 embedding，不能用“每个 speaker 几条参考”估算这一协议的存储。
  Galgame/Wenet 没有 speaker 标签不因此被排除；独立参考克隆评估另定。
- selection 保留基础行，在分支上记录原因/flags；codec 第一轮只消费 supervised_tts selection 的 reason=0。
  空音频、空文本和确认坏样本由数据规则排除。不是要求所有未来纯音频特征都必须有文本。
- codec、speaker 各一张 dataset/profile/run 的 features.lance；不按 GPU/batch 对外发布，不生成逐条 NPZ。
  不复制原始音频，不保存长期 mel，也不先物化第二份训练 codes。
- 生产 codec 采用 FA2 + BF16 候选 profile；FP32/eager 用于基线检查，两种 profile 不混存。
  speaker 先以 FP32 输出建立基线；ECAPA 并非 FA2 attention 模型，不能声称 speaker 已用 FA2 加速。
- 改模型/前处理/精度需新 profile；改采样权重只改 training_plan；换 feature 快照重建 locator 绑定。
  原始稳定 ID 保持哈希，profile_name/run/build/selection 名字可读且用北京时间 bjt。

## 本地模型与代码证据

权重已在 cache/feature-models，sources.json 记录真实文件 SHA256；不存在“尚未下载”的前置阻碍。
codec：Qwen/Qwen3-TTS-Tokenizer-12Hz，revision 7dd38ad4e9bad454aae9cd937d0cd577604fe229。
speaker 来源：Qwen/Qwen3-TTS-12Hz-0.6B-Base，revision 5d83992436eae1d760afd27aff78a71d676296fc。
配置确认 codec 输入 24k、downsample=1920、有效16码本、encoder vocab=2048；名义帧率12.5Hz。
speaker enc_dim=1024。模板仍有 frontend/length/reproducibility 待验参数，不能拿模板直接发生产 profile。

训练仓库 qwen3_train/data.py 的目标数据路径调用 audio_mel(row)，speaker.py 的 frontend 使用
24k、128 mel、n_fft/win=1024、hop=256、fmin=0/fmax=12000，非24k路径用 torchaudio resample。
codec 候选 scipy resample 与此不是同一种前处理；可共享原生解码结果，不默认共享重采样波形。
后续训练改用新读取器，不受旧 NPZ/manifest 行列表的接口限制；本轮未修改训练仓库。

安装版 qwen-tts 0.1.1 的 tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py：
encode 调用 encoder.encode 时不传 padding_mask，只在最后按 mask.sum()/1920 裁 codes。
Transformers 4.57.3 的 Mimi _encode_frame 也有 encoder 支持 padding 的 TODO。
models/modeling_qwen3_tts.py 的 ECAPA attentive pooling 将每条 lengths 设为1，视整个补齐序列为有效。
因此异长混批对边界/池化结果的影响必须实测，不能因为输出 T 正确就认为内容等价。

## 执行路径

1. **固定数据。** 发布完整 selection 后，校验各 base/branch/version/tag、全局重复裁决和规则哈希。
   打开固定分支，按 reason=0 顺序读取，只投影所需音频和身份列；目标集合摘要在推理前固定。
   小规模测试可额外固定少量 targets，输出在本仓库 artifacts/features-pilot，不放 unified。
2. **CPU 前处理。** 一次解码原始 bytes，检查帧数、采样率、有限值；本轮 sample 使用整条 `[0,num_frames)`。
   不因为 metadata 有旧录音坐标就再次裁剪。真正需要片段时先明确 view，不能截 token 或静默截前N秒。
   codec/speaker 分别按固定 profile 重采样/提 mel。mel 仅用于当前推理或有界临时缓存，不发布为长期特征。
3. **GPU 任务。** 后续授权执行时使用8卡，每卡一个持有模型的进程，不使用DDP同步梯度。
   按音频长度和总帧预算分发；CPU解码池、GPU微批、写入队列各有独立容量，监测吞吐后调并发。
   128 CPU workers 是可测试配置，不是固定使用128个GPU进程；避免盲目并发压垮共享盘。
   codec 与 speaker 可在同GPU进程消费同次解码，但采用各自的等长/批处理策略与独立结果检查点。
4. **FA2与数值。** 检查嵌套 encoder/transformer 的实际 attention backend，而非只看外层 config；
   保存 profiler 证据，禁止 fallback 后报告 FA2。先单条对照，再测试长短混合、batch重排、batch尺寸和重试。
   同 profile 的 codec 要整数完全一致；FA2 BF16 与 FP32 跨 profile 差异另报告并听检重建。
   speaker 按单条或严格等长 mel 推理；简单“接近长度分桶+补零”不能消除池化问题。
   若要实现有效长度 mask，先证明卷积边界与池化均等价，再作为新 frontend/implementation profile。
5. **输出与恢复。** 特征按完成顺序流式写；每个 dataset/kind/run 单协调者提交，GPU worker不抢schema。
   实际数组存 [T,16] int16、[1024] float32；无padding/特殊token。失败保留 target 和原因，不补零。
   manifest 记录 selection 范围、profile、成功/失败终态和实际软硬件。恢复键为 target+fingerprint，不能用worker号。
6. **训练绑定。** 在 selection 的派生 build 分支一次批量关联，添加 codec_row/speaker_row/build_ready。
   features可乱序，必须验 target_id、音频区间、profile 和status；固定 feature snapshot，批量take读取。
   换run新建绑定；同profile只补缺失目标时新建少量subset特征，build必要时增加run_slot，复用旧成功表；改权重新建plan。真实训练读取器和任意语言/质量 weighted sampler 仍需实现。

## 重复处理与存储预算

精确音频冗余约610万条/4.53%来自用户已有全量核查，最终仍要核对完整 SHA256。
选代表、文本冲突、评估隔离属于 selection 数据正确性，不建立全球内容存储服务来追求这4.5%的空间。
特征按数据集发布，允许跨来源少量重复；同run同feature_key复用已校验的权威数组，身份行仍独立记账。
不能按 speaker_id 共用 embedding，不能因纯文本改动让兼容音频特征失效。

按全部134,832,658条、373,638.02355小时估算，十进制GB，尚未扣selection排除与失败：

| 内容 | 原始数值体积 |
| --- | ---: |
| codec，12.5帧/秒 ×16码本 ×2bytes | 约538 GB |
| 每目标1024维FP32 speaker | 约552 GB |
| 两列int64特征定位 | 约2.16 GB |
| uint16原因+uint32 flags | 约0.81 GB |

表内多个身份/指纹、Lance元数据、索引、临时磁盘另计。每个64字符哈希字段全库约8.6GB未压缩，
不能把上述数组体积当完整磁盘需求；全量前用pilot测真实每条/每小时bytes并外推峰值。
FP16 embedding可约276GB，但必须独立profile验证误差和训练表现，暂不直接采纳。
在线speaker可省缓存但需要训练时音频I/O；保留给解冻路径，不作为当前自动替代。
不常驻一份带完整text的catalog，不按采样配方反复复制features。短期保留少量有效实验run，其余按依赖回收。

## 验收与放量门槛

先用 LJSpeech/CSEMOTIONS 的固定少量样本做正确性测试，再从每个dataset/语言/长度/采样率/声道抽样。
LJSpeech单语不能替代多语言分布验收。记录输入音频秒数/墙钟秒、条/秒、GPU空闲、解码/重采样/
编码/speaker/写入各阶段耗时、峰值VRAM/RSS、共享盘读写与最终bytes；预热与正式统计分开。
总耗时按实测端到端音频秒/秒估算，不能只用GPU kernel时间乘总量。

必须通过真实权重hash、FA2 backend、codec长度/值域/重建听检、speaker在线对照、异长/等长批、
坏输入记账、Lance回读、8卡覆盖无重无漏、断点换worker、绑定身份/固定快照、实际训练吞吐与采样分布测试。
数据selection发布与特征pilot可分线准备；全量特征必须等数据范围固定和上述可行性验收。
这轮不启动pilot/GPU提取；先完成方案、文档和临时存储验证。
