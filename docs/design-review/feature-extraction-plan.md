# Codec / speaker：当前决定与实施门槛

截至 2026-09-29，selection 已发布，C codec 已通过八卡数值核验、持久化/恢复验证和用户试听，
正式全量执行已启动。运行入口见 [codec](../codec.md)，证据与历史比较见
[数值验证](codec-inference-validation.md)。本轮没有修改 LM-TTS-Training。

## 当前实施

首版从头训练 Qwen3-TTS 结构，使用冻结的官方 tokenizer；speaker 条件采用目标自身音频（self）。
每个入选目标需要自身 embedding，不能按“每个 speaker 几条参考”估算。
Galgame/Wenet 不因缺 speaker 标签被排除；独立参考克隆评估另定。

codec 消费固定 supervised_tts selection 的 reason=0，共 128,220,178 条、357,506.84 小时。
范围已经包含 1–120 秒限制、缺失文本/音频检查、精确重复与冲突裁决、首尾空白和语言别名规则。
未来纯音频特征可以有其他选择范围，不把本轮文本条件写入音频特征身份。

当前只有 C：FP16 encoder、FA2 全因果注意力、FP32 归一化码本缓存与量化距离。
有效长度 mask、固定桶和 canonical batch 形状已在仓库实现；不使用上游未经处理的异长补零路径。
使用 8 卡 × 每卡 2 个进程 × 4 解码线程；batch 最大 64，由目标长度确定，不能因空闲显存或 OOM 改形状。
A/B 已停止并清理；保留比较结论，不复用其 codes。具体数值参数由生产 profile 固定。

权重在 cache/feature-models，sources.json 保存文件 SHA256：

- codec：Qwen/Qwen3-TTS-Tokenizer-12Hz，revision `7dd38ad4e9bad454aae9cd937d0cd577604fe229`。
- speaker 来源：Qwen/Qwen3-TTS-12Hz-0.6B-Base，revision `5d83992436eae1d760afd27aff78a71d676296fc`。

codec 输入 24 kHz、downsample=1920，有效帧率 12.5 Hz；16 码本，每码本 2048，存 int16 `[T,16]`。
音频完整解码后核对帧数、采样率和声道，再取均值、按 profile 重采样；metadata 中原录音坐标不重复裁剪。
首版只处理整条 sample，其他区间需要先实现并验证 view。

每个 dataset/release/kind/run 发布一张 features.lance，路径没有多余的 profile 哈希目录层。
不按 batch/GPU 发布，不生成逐条 NPZ，不复制原音频，不持久化 mel。
worker 写未提交 fragment；检查点固定任务成员和文件 hash，协调器按计划顺序发布并检查完整覆盖。
未知错误停止，保持 incomplete；不能靠丢目标继续发布。正式输出和恢复方式见 codec 执行说明。
发布前把最终选用 text/language、文本版本、来源及 speaker 元数据追加到 codec 表，逐行核对 target_id。
当前执行器只支持基础文本及其规范化；annotation 修订原文的绑定尚未实现，显式拒绝。

## Speaker 与训练仍需完成

已完成 speaker 初探：FP32 ECAPA 输出 1024 维，单条/跨卡、在线同权重对照、Lance 回读与梯度通路已验证。
严格等长批通过，异长补零批未通过；ECAPA 池化没有有效长度参数，不能套用 codec 的 FA2 优化。
下一步需验证单条/严格等长的真实吞吐、完整 TTS loss 与音色克隆效果，之后才能发布 speaker profile 并全量提取。
本轮未启动 speaker 全量；候选模板不表示生产验收通过。

speaker 前处理沿用训练的 audio_mel：torchaudio 重采样与 codec 的 scipy resample_poly 不同。
可以共享原生解码，不能默认共享重采样后的波形。mel 仅作为推理临时输入；解冻时按当前 frontend 在线计算。

训练以含文本的 codec 表为主，从固定快照建 build 分支，按身份核验后增加 speaker_row/build_ready。
只改采样权重时新建 training_plan，不再复制特征。多 run、在线路径和缺失覆盖遵循 07/11。
绑定构建器、新训练读取器、通用多语言质量加权 sampler、训练吞吐与评估隔离仍需实现/验收。
这些是训练放量门槛，与已经通过的 codec 提取验收分开。

## 存储预算与清理

以下按全部基础 134,832,658 条、373,638.02 小时估算，未扣 selection 排除，单位为十进制 GB：

| 内容 | 原始数值体积 |
| --- | ---: |
| codec，12.5 帧/秒 × 16 × 2 bytes | 约 538 GB |
| 每目标 1024 维 FP32 speaker | 约 552 GB |
| 单列 int64 speaker_row | 约 1.08 GB |
| uint16 原因 + uint32 flags | 约 0.81 GB |

哈希、文本、Lance 编码/索引、null bitmap 与临时文件另计；数组体积不是最终磁盘需求。
每个 64 字符哈希字段全库约 8.6 GB 未压缩；文本及其他小列也有数十 GB 量级成本。
FP16 embedding 可减半，但需新 profile 和误差/训练验收，暂不直接采纳。

当前 selection 已统一裁决精确重复，首版 codec 拒绝同 run 出现重复 feature_key。
通用同键权威结果复用协议存在于 contract，当前执行器不宣称支持任意重复目标输入。
跨数据集不引入全局特征存储服务；纯文本变化不使兼容音频特征失效。
清理保留当前 runtime、模型、plan/checkpoint、验收 JSON 及引用快照；可重建实验音频/张量已移除。
