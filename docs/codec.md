# Codec：BF16 生产入口、验证与运行

**当前状态：全量启动已按用户要求暂停，尚未启动新的全量任务。**
默认代码为 `qwen3_12hz/encoder.py` 的 packed BF16 实现；FP32 目前只做小规模实验。
已经删除长度 policy、逐长度 JSON、实验候选继承链和旧 FP16 入口。
卷积/线性层使用固定计算方式，没有外部 padding；公式与官方模型一致，
但底层浮点累加顺序不同，因此不要求与官方单条推理逐 token 相同。
当前 509 条对照为 119,477 / 1,140,240 个 token 不同（10.48%），不是音质下降比例。
之前的“18,060 条零差异”属于已归档的长度对齐版本，不能用于当前固定计算版本。
详见 [BF16 实验](design-review/codec-bf16-experiments.md) 和
[FP32 小测](design-review/codec-fp32-check.md)。

未来全量运行需要新 profile、冻结 runtime 和独立输出路径；不得复用旧 C codes 或检查点。
不要求对新数据集穷举计算形状；没有未覆盖长度的补查或拒绝逻辑。
旧 C 已退役，其 unified 输出和状态已按要求删除。

首轮固定 selection `tts-selection-supervised-tts-20260929T151539bjt-01` 的 `selection_reason=0`：
16 个数据集，128,220,178 条，约 357,506.84 小时。原始音频和 selection 不重写。

历史 C 于 **2026-09-29 18:25:52（北京时间）** 启动，现已退役，不能续跑。
运行目录为 `artifacts/codec-runs/qwen3-codec-c-20260929T182519bjt-01/`；使用 8 卡 × 每卡 2 进程 × 每进程 4 解码线程。
A 已停止，B 未启动；A/B 的结果、检查点、运行副本和过时试验工具已退役。
不要使用旧 A/B 的 run 路径恢复，也不能将其 codes 作为 C 的结果复用。

## 代码职责

实现集中在 `src/tts_data_pipeline/codec/qwen3_12hz/`，对应 Qwen3-TTS-Tokenizer-12Hz。
未来其他 tokenizer 放在 `codec/` 下各自独立的子包；顶层 `codec/__init__.py` 仅惰性兼容旧公共 API。

| 模块（子包内） | 职责 |
| --- | --- |
| `encoder.py` | 固定软件栈、官方 BF16 加载、packed 推理及有界审计计数 |
| `bf16/model.py` | 直接组织 CNN、Transformer、下采样和 RVQ，无实验候选继承链 |
| `bf16/convolution.py`、`linear.py` | 变长卷积与矩阵乘法 kernel |
| `bf16/quantization.py` | BF16 量化与边界复核 |
| `audio.py` | 原生解码、哈希/时间轴检查、声道处理、重采样、codes 验证 |
| `profile.py` | 权重校验、数值 profile、实现摘要与特征身份 |
| `schedule.py` | 纯 CPU 长度桶和任务索引，不依赖 torch/FA2 |
| `run.py` | 计划、预取、worker、检查点、恢复、校验和发布 |
| `text.py` | selection 选用文本/语言的物化与校验 |

`scripts/extract_codec.py` 保留 CLI 入口，明确导入此 tokenizer 的 `run` 模块。
旧 FP16 入口、补齐实现和实验中间类已从 src 移除，历史追溯使用 artifacts 中冻结的 runtime。

`Encoder.encode_single()` 使用与批处理相同的 BF16 路径，无外部补齐。
准确度参考必须独立调用未修改的官方 `Qwen3TTSTokenizer`，显式指定 BF16。
`schedule.py` 的长度桶只决定任务分组；新 encoder 不向音频或 batch 尾部添加零样本。
模型自身的因果卷积与 stride padding 按官方定义保留。

BF16 profile 固定全部数值源码、第三方依赖和权重摘要，并明确不承诺官方逐 token 等价。
生产验收要求 `official_bf16_accepted`、`production_codec_accepted`、匹配的 profile_id
和可核验的证据摘要。旧 FP16 acceptance 不适用于新入口。
GPU worker 保留量化复核累计计数并写入 checkpoint，避免逐 batch 审计列表长期增长。
运行时记录硬件；恢复时核对源码、依赖和已固定 profile。

以下 C 运行、性能和试听记录均为历史证据，不代表当前 BF16 验收。

## 历史 C 数值路径

所有修改位于本仓库；不修改 `site-packages`，不将本项目安装为 distribution。
本次已复制仓库源码为固定 runtime，文件摘要记录在运行目录的 `runtime-files.json`。
第三方源码、依赖版本及模型权重也固定摘要。

冻结运行的 profile 名为 `qwen3-12hz-24k-k16-fp16-fa2-canonical-v1`，历史实现 ID：
`917f86ce31fed41976e6a065b0a3ecc6826337006268370109be80984fbcee67`。
模型为冻结的 Qwen3-TTS-Tokenizer-12Hz；24 kHz、16 码本、每码本 2048、int16 `[T,16]`。
FP16 卷积/Transformer，FA2 全因果注意力，FP32 码本缓存与量化距离计算。
2 秒长度桶，固定批大小最大 64、padding 后音频预算 480 秒；不是根据当前空闲显存动态改 batch。

## 已完成的端到端对照

AISHELL-3、LJSpeech、CSEMOTIONS 入选全集共 105,273 条、119.7314 小时。
两轮输出的 codes hash 与全部选用文本/语言/文本版本一致，并独立回查固定 selection。

| 每卡进程 / 解码线程 | 编码阶段（含启动与 IO） | 完整校验发布 |
| --- | ---: | ---: |
| 1 / 8 | 84.94 秒 | 95.19 秒 |
| 2 / 4 | 66.56 秒 | 78.98 秒 |

正式任务采用 8 卡 × 每卡 2 进程 × 4 解码线程。此次为共享 GPU 上的小数据集对照，
不能直接作为 35.75 万小时全库的工期保证；全量启动后需按实际 checkpoint 窗口观察。
临时试跑数据已清理，保留比较结果、各数据集 manifest 副本及验收摘要。

## 验证与启动

验收目录：`artifacts/codec-validation/c-fp16-fa2/`。
包含八卡码字/边界验证、profile、端到端吞吐对照、清理清单；用户试听目录为
`artifacts/codec-listening/c-fp16-fa2-20260929T1804bjt/`。

一次性样本准备和验收脚本已清理；抽样、边界、跨卡与混批核验步骤见
[验证方法](design-review/codec-inference-validation.md)。保留验收 JSON、试听结果摘要与固定目标清单；试听音频、复制的 fixture 与实验 speaker 张量已清理。
这些记录证明本次 profile 的验收范围，不能代替未来修改实现后的重新验收。


本次固定的 plan、preflight、runtime 和 launch 记录均在 `artifacts/codec-runs/qwen3-codec-c-20260929T182519bjt-01/`。
`reports/codec/active.json` 保存 PID、完整命令和源码/计划摘要；日志为 `reports/codec/production-c.log`。
旧输出已按用户明确要求清理，旧恢复命令撤下。删除了 16 个 codec 输出目录、3 个对应状态目录，
共 2,245 个文件、15,792,937,872 字节。原始数据、selection 与其他 feature 类型未清理。
清单与删除结果位于 `artifacts/codec-padding-review/unified-cleanup/`；旧运行目录已写入 `RETIRED.json`，
`reports/codec/active.json` 标记 `retired_outputs_deleted` 和 `resumable=false`。
以下存储布局仅用于解释历史实现。

固定任务成员沿用已核对的 selection 目标清单，不重新扫描音频；每个 worker 仍验证任务的
有序 sample_id 摘要、原始音频哈希和时间轴。复用的只有目标清单，不复用 A/B 的 codes。

最终输出位于 `/workspace/data/DATA-TTS-UNIFIED/datasets/<dataset>/v0.1/features/codec/<run_id>/`。
未发布输出使用 `.incomplete`，checkpoint 在该 dataset 的 `.state/v0.1/features/codec/<run_id>/`。
2026-09-29 18:36（北京时间）移除多余的 profile 哈希目录层，保留 310 个检查点、1,072,750 条结果并续跑。
profile 哈希仍存在于 manifest/行中；迁移清单位于运行目录的 `migrations/readable-run-layout-v1/`。
每个 dataset 对外发布一张 `features.lance`，不会按 GPU/batch 发布多张表。
编码完成后协调器校验覆盖、追加选用文本、建立索引并发布；编码完成不等于已经发布。

## 恢复与保留

旧 C 的 unified 输出和 checkpoint 已删除，不能恢复此旧任务。未来新任务恢复时须逐个核对对应检查点。
OOM 或数值错误时停止，不通过减小 canonical batch 或静默丢弃目标继续运行。
修改数值实现须创建并验收新 profile；并发和预取配置变化也需要核对结果一致性。
保留本地运行目录、验收 evidence、模型 cache 和固定快照供审计；旧 unified `.incomplete/.state` 已清理。

当前仅 codec。最终表包含 text、language、文本版本、来源和说话人元数据；
speaker embedding 另行验收，后续通过训练 build 分支绑定 locator。
约定见 [06](data-contract/specs/06-codecs.md)，技术证据见 [验证记录](design-review/codec-inference-validation.md)。
