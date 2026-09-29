# Codec C 验收与训练接口

2026-09-29，北京时间。当前正式方向为 **C：FP16 / FA2 全因果注意力 / canonical batch / 16 码本**。
用户已试听并授权全部切换 C，淘汰 A/B。C 不是 FP32 A/B 的逐码字等价实现，全部重新编码。
正式状态和恢复入口见 [codec](../codec.md)。

## C 的数值与运行边界

- 398 条真实音频覆盖 16 个数据集，加 14 条长度边界，共每卡 412 条；8 卡逐码字相同。
- 顺序反转、替换邻居、不同槽位、单条与尾批均逐码字相同。
- 6 条用户试听样本（中/英/日/德/荷）与正式实现逐码字相同。
- 2 秒长度桶，canonical batch 最大 64，最多 480 秒 padding 后音频；形状只由目标长度决定。
- 卷积逐层传播有效长度；下采样复制各样本有效末尾；FA2 显式使用全因果窗口。
- 模型先载入 FP32，显式物化 FP32 码本缓存，再将 encoder 转 FP16。
  这是试听脚本先做 FP32 参考编码留下的真实语义；原试听报告的
  `float32_on_fp16_codebooks` 描述不准确，实际为 `float32_on_cached_fp32_codebooks`。
  该问题通过冷启动对照暴露并修正；没有用另一种冷启动 FP16 结果替代试听版本。
- 以上逻辑位于本仓库 codec.py / codec_batch.py / codec_fast.py；没有修改第三方安装包。
  验收、profile、冻结 runtime 均包含本地实现摘要，第三方权重与源码作为只读依赖固定。

验收文件保存在 `artifacts/codec-validation/c-fp16-fa2/`：八份 gpu 报告、summary、profile、
端到端报告与 acceptance。用户试听凭据在 `artifacts/codec-listening/c-fp16-fa2-20260929T1804bjt/`。
412 条单卡重复编码耗时 2.58–3.65 秒，仅代表该测试集的纯推理，不包括全库 IO 和写入。
端到端对照覆盖 AISHELL-3 / LJSpeech / CSEMOTIONS 共 105,273 条、119.7314 小时；
每卡 1 进程完整发布 95.19 秒，每卡 2 进程 78.98 秒。码字与文本等元数据全量一致，
并核对基础 selection。建议正式启动先用每卡 2 进程、各 4 解码线程。
真实全库吞吐仍以正式 checkpoint 的观察窗口为准，不按纯推理线性外推承诺工期。

## 训练仓库实际使用的模型

只读检查 `../LM-TTS-Training`，commit `87e026770e6c94c0e912b2e678600611aa558af5`：

| 组件 | assemble_qwen3_tts.py 的默认来源 | revision |
| --- | --- | --- |
| 文本 backbone | Qwen/Qwen3-0.6B-Base | da87bfb608c14b7cf20ba1ce41287e8de496c0cd |
| TTS 配置、文本前端、speaker encoder | Qwen/Qwen3-TTS-12Hz-0.6B-Base | 5d83992436eae1d760afd27aff78a71d676296fc |
| 音频 tokenizer | Qwen/Qwen3-TTS-Tokenizer-12Hz | 7dd38ad4e9bad454aae9cd937d0cd577604fe229 |

`qwen3_train/assembly.py:audit_sources` 要求 16 个 code groups、每个词表 2048。
`data.py` 同样要求 codes 为 `[T,16]`，整数范围 `[0,2048)`。
Talker 第一输出头的 3072 词表包含控制 token，不能据此把 codec 的合法值域放宽到 3072。
codec 的第一个码本承载 semantic codes，后面 15 个是 acoustic residual codes。
输入为 24 kHz；hop 为 1920，名义帧率 **12.5 Hz**，不是精确 12 Hz。
输出按 encoder 实际返回的 T 验证，再核对 `ceil(input_frames / 1920)`。
存储 int16；训练读取时转为 torch.int64，不向持久化数组添加 BOS/EOS 或 batch padding。

speaker 来自模板权重的 `speaker_encoder.*`，ECAPA-TDNN，24 kHz，输出 1024 维。
本轮只提取 codec；speaker 验收、mel 前处理和数值精度另行固定。
当前训练代码 `data.py` 对目标音频调用 `audio_mel(row)`，对应已确认的 self-reference 协议。

**初始化差异必须保留：** `initialize_model` 会加载文本 backbone 的 layers/norm；
assemble 默认还加载 TTS 文本 embedding/projection 和 speaker encoder。
这不是用户希望的完全重新初始化的 base。此次只借用 tokenizer/接口来源，不修改训练仓库，
也不把旧 assemble 的权重初始化流程当作后续训练决定。
`copy_codec` 将 tokenizer 文件复制到 `speech_tokenizer/`，不重新初始化 tokenizer。
这里核对的是代码声明的 revisions；训练仓库未见对应本地 assembled checkpoint，未声称核对过其权重文件。
本仓库 `cache/feature-models/sources.json` 则已逐文件核对实际 SHA256。

## 执行器与发布边界

`scripts/extract_codec.py plan/run` 固定 selection manifest、branch/version、目标集合、profile、
执行代码和验收证据。输出仍按 dataset/profile/run 发布一张 `features.lance`。
目标 task 包含 fragment、筛选后 offset/count 和有序 sample_id 摘要；GPU 分配不影响身份。
同一 C profile 更换 IO/进程并发时可复核 checkpoint 续跑，不能改变 canonical batch 形状。

任务内按长度桶预取并编码，结果恢复为固定的原始任务行序。最多预取两批解码波形。
发布前从同一个固定 selection 追加最终 text、language、文本版本及来源/说话人信息。
worker 验证原始 bytes 哈希和时间轴；不按 metadata 的录音坐标再次裁剪。
每批完整回读，逐行核对 payload hash、长度、值域和输入指纹，之后才持久化 checkpoint。
发布再次核对文件哈希、目标覆盖和身份唯一性，再建索引、固定版本、原子重命名。
存在解码、输入、数值或显存异常时停止，不减 batch、不补零、不静默丢弃样本。

A/B 的失败试验和逐阶段运维日志不再作为活动文档维护。历史结论保留在本文和
[padding 原理](codec-padding-review.md)；旧数据、源码副本及报告的清理清单保存在 C 验收目录。

## 已验收 C 实现参数（从 contract 移入）

### 模型与前处理

参考当前训练实现，首个正式 C profile 固定如下；具体实现摘要与验收证据由每次 run 固定：

| 项目 | 候选 |
| --- | --- |
| codec | Qwen3-TTS-Tokenizer-12Hz；使用冻结 tokenizer，Talker 重新初始化与 codec profile 无关 |
| 波形 | 24,000 Hz 单声道 float32；原生坐标先切片，显式重采样 |
| 输出 | [T,16]，16 个码本逐个值域 [0,2048)，存 int16 |
| 推理 | C：卷积/Transformer FP16 + 实际 FA2，全因果注意力；FP32 码本缓存和距离计算；eval + inference_mode |
| 幅度/静音 | 不额外归一化、去静音、裁短或增强 |
| 重采样候选 | scipy.signal.resample_poly，gcd 约分，Kaiser beta=5，constant/cval=0；固定版本与输出长度规则 |

候选采纳上述重采样策略时 N=ceil((end-start)*24000/native_sample_rate)，24k 输入直接保留；
需用实际前处理实现验证此规则。不能在 profile 写 scipy，而实际走 codec.load_audio 的另一重采样器。
已下载 config 的 encode_downsample_rate=1920、采样率=24000，名义帧率实际为 12.5 Hz；
单条 T 仍取 encoder 的实际有效长度，不用时长乘帧率取整。模型已下载不等于完成数值/批处理验收。
生产 profile 固定实际 attention 后端。采用 FA2 时明确设置 flash_attention_2，检查实际模块类
与 profiler；不允许静默回退后仍报告 FA2。PyTorch SDPA 内部的 flash kernel 不等于外部 flash-attn 2。
Qwen 0.1.1 / Transformers 4.57.3 的 Mimi 必须在实例化前设置嵌套 config；实例化后仅改变配置
可能保留旧 attention 类，却改变 mask，不能以配置字符串单独验收。
所有后端的不同长度混批都必须核对 encoder 的 mask/长度实现；等长也要测数值一致性。
未通过验证时采用单条推理；批处理吞吐不能凌驾于已固定的数值/边界语义之上。
FP16/BF16/FA2 和 FP32 不保证量化 token 相同：差异必须报告和听检；不能用基线结果冒充 FA2 profile 缓存。
同一生产 profile 的 batch 重排/重试则要求 codec 整数精确一致；不能做到时固定分块/长度处理语义或退回逐条路径。

### 数值边界

- 输入仍为整个基础样本的单声道 24 kHz 波形；本次 selection 为 1–120 秒，不裁短长音频。
- 以 48,000 帧（2 秒）向上取整得到 bucket；canonical batch 大小为
  `floor_power_of_two(min(64, floor(480*24000/bucket_frames)))`。
  单条目标的 batch 形状只取决于自身长度；尾批仍填足同一大小，空槽为有效长度 1 的零波形。
- 卷积逐层维护有效长度，stride 边界清零无效位置；downsample 的 replicate padding
  复制该条音频最后一个有效值。仅在最终输出截断不能代替这些规则。
- attention 固定全因果窗口。安装版本的 Mimi FA2 默认局部窗口与 FP32/SDPA 基线不一致，
  C 必须显式关闭局部窗口；不能悄悄恢复默认 sliding_window。
- 试听程序曾在转 half 前运行 FP32，Mimi 的惰性 `_embed` 缓存因此保持 FP32。
  正式实现必须在 FP32 权重上显式计算 `embed_sum / clamp(cluster_usage, min=1e-5)`，
  再转 encoder 为 half，并保留该 FP32 缓存；距离计算为 FP32，残差在减去 FP32 量化值后提升为 FP32。
  “整个码本 FP16”不等价于本次通过试听的 C。profile 必须记录缓存初始化顺序及实际计算精度。
- 不按当前空闲显存改变 canonical batch；OOM 必须停止，禁止减 batch 后继续写同一 profile。
  进程数、CPU 预取与 IO 并发可调整，但不得改变上述数值形状。
- 每个新实现验收：试听样本逐码字相同、跨 8 卡一致、邻居/槽位/尾批/顺序变化一致，
  覆盖 1 秒至 120 秒及 bucket 边界；真实流水线验证目标覆盖、文本、持久化回读和吞吐。
- C 与旧 FP32 A/B 的码字不同；不得搬用 A/B payload、检查点或 profile 身份。
  用户已批准 C 后全量重新编码；清理旧产物必须记录清单，不能影响基础表、selection 或模型 cache。


## 一次性验证代码清理后的方法记录

样本准备：在固定 selection 的每个 dataset 均匀选取最多 2048 个逻辑位置，只保留 reason=0 的候选。
按选用语言、原采样率、声道和时长桶（2/5/15/30/60 秒边界）分组，取各组观察到的首尾样本，
再补观察到的最短/最长各两条，按位置去重，得到 398 条。该策略不是全库质量抽样保证。

C 数值核验：对完整波形调用同一前处理和 Encoder.encode_many，保存每个目标的 codes_sha256；
将顺序反转重算，再对每种 bucket 比较单独目标、填满邻居和不同槽位。各卡完整运行并比较全部摘要。
边界长度（24 kHz 帧）：24000、24001、47999、48000、48001、239999、240000、240001、
719999、720001、1439999、1440001、2879999、2880000。边界使用真实波形重复构造，仅用于实验，
生产绝不据此补齐/裁剪目标。6 条试听码字摘要在原试听报告及八卡验收报告中对应。
端到端另核对每个目标的代码摘要、文本、语言、text_revision、text_source、覆盖数和持久化回读。

清理了 prepare_codec_probe、validate_codec_c、probe_speaker 及 selection 存储 benchmark 等一次性脚本。
tests 保留生产逻辑回归：波形身份、码值范围、固定 batch、FP32 缓存、检查点与文本发布。
GPU 验收摘要仍保留；后续修改数值实现时须重新进行真实权重/八卡核验，不能用 CPU 单测替代。

## 已完成的 speaker 探查（非生产验收）

同一 398 条、16 来源；GPU 6/7；FP32 ECAPA 输出 1024 维。与训练 audio_mel 的前处理逐值一致。
声道取 float32 均值，torchaudio sinc_interp_hann 重采样，lowpass_filter_width=6、rolloff=0.99。
mel：24k，n_fft/win=1024，hop=256，128 bands，fmin=0/fmax=12000，center=false；
反射补帧各 384，幅度 epsilon=1e-9，Slaney/non-HTK mel，自然对数下限 1e-5；不持久化 mel。
固定权重单条重复/跨卡比较、在线同权重参考、Lance float32 回读和参数梯度通路通过。
比较容差 atol=1e-5、rtol=1e-4；相对误差以固定权威结果为基准。
严格等长批测试 16/16 通过，异长补零批只有 1/16 通过（最大绝对差约 1.2954），不能直接混长批上线。
尚未做完整 TTS loss/音色克隆评估或生产吞吐验收；production_accepted 仍为 false，未启动 speaker 全量。
保留 profile-candidate/report 的参数和结果摘要，已删除一次性代码与可重建实验产物。
