# Codec：BF16 / FP32 生产入口、验证与运行

正式默认使用 **FP32 + TF32x3 补偿乘法**，保留原有 BF16 路径。
`Encoder(model_root, precision="fp32")` 为默认；`precision="bf16"` 选择原有 BF16。
CLI 计划阶段使用 `--precision fp32|bf16`，精度写入 plan/profile，恢复时不能切换。

FP32 的权重、激活、累加和 RVQ residual 均为 float32；Triton 矩阵乘法使用
三项补偿的 TF32x3，原生 PyTorch matmul / cuDNN 的普通 TF32 关闭。
这不是严格 IEEE FP32 乘法逐位等价的承诺。卷积权重按固定归约顺序预重排并缓存，
减少非连续访存；BF16 保持此前的权重布局、计算及类型转换。
精度标准是未修改官方 tokenizer 的 **同 dtype、单条、实际长度** 推理。
离散 token 差异不是音质下降比例。验收与满载吞吐见
[FP32 优化验证](design-review/codec-fp32-optimization.md)。

不使用长度 policy、逐长度 JSON 或外部 padding。新数据集不需要穷举计算形状。
模型自带因果/stride/replicate padding 按定义保留；selection 当前只选整条 sample。
BF16 历史对照和早期 FP32 小测见 [BF16 实验](design-review/codec-bf16-experiments.md)
和 [FP32 小测](design-review/codec-fp32-check.md)。此前长度对齐实验的零差异结果
不代表目前无 policy 的实现。

全量使用新 profile、冻结 runtime 和独立输出路径，不复用旧 C codes 或检查点。
当前运行位置和进程记录以 `reports/codec/active.json` 为准。
固定 selection 为 `tts-selection-supervised-tts-20260929T151539bjt-01` 的
`selection_reason=0`：16 个数据集，128,220,178 条，约 357,506.84 小时。
原始音频和 selection 不重写。旧 C 已退役，其 unified codec 输出及状态已删除。

## 代码职责

实现集中在 `src/tts_data_pipeline/codec/qwen3_12hz/`，未来其他 tokenizer 使用各自子包。
顶层 `codec/__init__.py` 仅惰性兼容公共 API；没有重复的 BF16 / FP32 模型实现。

| 模块 | 职责 |
| --- | --- |
| `encoder.py` | 精度选择、官方权重加载、packed 推理、有界审计计数 |
| `packed/model.py` | CNN、Transformer、下采样与 RVQ |
| `packed/convolution.py`、`linear.py` | 变长卷积、矩阵乘法、FP32 权重缓存 |
| `packed/quantization.py` | 同 dtype residual 链、距离计算和边界复核 |
| `audio.py` | 解码、时间轴/哈希、声道处理、重采样与 codes 验证 |
| `profile.py` | 精度、计算模式、源码/依赖/权重摘要与特征身份 |
| `schedule.py` | 长度分组和任务索引，最多 64 条 / 480 音频秒 |
| `run.py` | 计划、worker、检查点、恢复、验证和发布 |
| `text.py` | selection 选用文本/语言的物化与校验 |

生产验收要求对应精度的 `official_fp32_accepted` 或 `official_bf16_accepted`、
`production_codec_accepted`、相同 profile_id 和可核验的证据摘要。
“accepted”表示已测数值差异和吞吐可接受，不表示官方逐 token 零差异。
`Encoder.encode_single()` 和批推理使用同一路径；官方参考需独立加载 tokenizer。
运行配置使用 8 卡 × 每卡 2 worker × 每 worker 4 解码线程，计划冻结实现与依赖摘要。

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
