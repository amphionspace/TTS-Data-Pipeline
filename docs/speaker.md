# FP32 speaker embedding 提取

入口为 `scripts/extract_speaker.py`，实现集中在 `src/tts_data_pipeline/speaker/qwen3_ecapa/`。
提取 Qwen ECAPA 原始 1024 维输出，不做 L2 归一化；用于冻结 speaker encoder 的离线条件。
训练仓库的离线读取和 loss/梯度集成尚未在这里实现。

## 输入与数值规则

- 复用 codec 已核验的 selection 目标计划，固定原始表的 branch/version、任务 ID 和目标集合摘要；不依赖 codec 输出。
- 使用完整实际解码波形。头信息 `num_frames` 只作调度提示；成功行的 `end_frame` 和输入摘要记录实际长度。
- FP32 声道均值和原生 torchaudio sinc 重采样到 24 kHz；缓存 FP32 重采样核。
- 解码使用 Linux 内存文件。仅 Opus 使用经过波形摘要对照的系统 libsndfile；其他格式保留原包的库。profile 固定库摘要。
- GPU FP32 mel，随后拼接真实帧；逐样本 offsets 隔离反射边界、SE 统计和 attentive pooling。没有 waveform/mel batch padding、裁剪或分段。
- CNN 使用补偿 TF32x3，最终投影保持原生 FP32；普通 Torch/cuDNN TF32 和 autocast 关闭。
- 原生单条 FP32 是独立参考。GPU FFT、矩阵乘法和分段归约次序可能产生小幅数值差异，逐元素验收固定为 `atol=1e-5, rtol=1e-4`。

`encoder.py` / `audio.py` 保留原生参考路径；`packed_encoder.py` 负责有界预取与组批，
`decoder.py` / `frontend.py` / `mel.py` 负责前处理，`packed.py` 实现真实帧计算。
`profile.py` 固定数值协议与身份，`storage.py` 列式存储向量并校验，`run.py` 负责恢复与发布。
本次生产未采用实验中的 attention 投影分解、更大 batch 或可变形状 policy。

## 验证与性能

实验保存在 `artifacts/speaker-acceleration/`，不写入源码目录。

| 验证 | 结果 |
| --- | --- |
| 496 条、16 个数据集的 Opus/原库分流解码 | 波形逐值一致 |
| 89,303 条逐条对照原生等长 FP32 基线 | 全部通过固定容差；正式代码重新验证通过 |
| 524,346 条按数据集占比分配、七卡与 codec 同跑 | 232.02 秒，约 2,260 条/秒 |
| 上述连续测试中的 2,441 条原生单条参考 | 全部通过，最大绝对差 7.15e-6 |

连续测试包括读取、解码、重采样、推理、写入和完整读回校验；不计进程初始化及额外原生参考推理。
按 128,220,178 条外推，提取约 15.76 小时。全库索引与发布另计，共享资源负载也会影响实际耗时。
正式 profile 与试验 profile 的代码摘要不同；验收文件绑定正式 profile，以及仅涉及 null 读回修复的 storage 摘要迁移证据。

## 计划、执行与恢复

在 `tts-features` 环境运行。计划需要已通过的 acceptance JSON，不能把 profile 示例当作可执行配置。

```bash
python scripts/extract_speaker.py plan \
  --targets-plan artifacts/codec-runs/<codec-run>/plan.json \
  --work artifacts/speaker-runs/<speaker-run> \
  --acceptance artifacts/speaker-acceleration/launch-acceptance.json

python scripts/extract_speaker.py run \
  --work artifacts/speaker-runs/<speaker-run> \
  --gpus 1 2 3 4 5 6 7 --decode-threads 16
```

一个 GPU 一个 worker，最多 64 条真实样本、90,000 mel 帧的调度预算，解码预取有界。
共享任务队列自动分配工作，每个 worker 最多读前一项、写后一项。
每个任务写未提交 Lance fragment，完整读回并验证数组与身份摘要后才原子写检查点。
中断后重跑同一命令，先校验已完成文件摘要，再续跑缺失任务；OOM/损坏文件会显式停止，不补零或跳过。
`--max-tasks` 用于 artifacts 内的断点恢复冒烟测试；限制任务数时不会发布不完整数据集。

发布到 `datasets/<dataset>/v0.1/features/speaker_embedding/<run-id>/features.lance`。
发布前再次核验文件、目标集合、唯一 feature key 和完整覆盖，建立精确 ID 索引并固定快照；不创建 ANN 索引。
失败行以明确 error_code 保留，manifest 汇总覆盖和错误数。

全量启动时冻结 `src/` 与运行脚本到 work 的 `runtime/`；后续恢复应使用该冻结入口，避免工作区修改改变运行实现。
实际工作目录、PID 与日志保存在 `reports/speaker/active.json`；进度以 work 的 `status.json` 为准。

## 随机 reference 提取

新增 `--reference-sources-plan <已有合表计划>` 和 `--reference-seed 20261008`。
计划仅从已有合表计划取得各数据集的独立 codec manifest/固定快照，不读取旧 merged payload。
`--targets-plan` 使用覆盖全部 16 个数据集的原 speaker 目标计划；`--acceptance` 必须绑定新的 reference profile。
具体坐标和采样规则见 data-contract/specs/11-speaker-embeddings.md。

`reference.py` 集中实现采样、坐标验证和推理前采样计划持久化，其他提取、检查点与发布流程复用原实现。
`run --memory-fraction 0.07 --mel-frame-budget 12000` 可以限制共卡提取的显存与组批预算；
它们只控制资源调度，不改变输入区间或精度。长 reference 独立组批，仍不足则显式报错，不截短或补齐。
本模式不限制绝对 reference 时长，实际资源与吞吐需按目标数据及共卡负载验证。

2026-10-08 reference 验证记录：

- 扫描 16 个数据集、128,220,178 条 codec 的实际长度；125,561 条无合法 reference，其余 128,094,617 条具备合法区间，最终有效数以实际提取为准。
- 最终实现对 3,764 条短、中、长及边界样本验证，3,568 条成功结果全部通过原生单条 FP32 容差；覆盖历史 42 条解码长度差异。最大 reference 43.28 秒。
- 全速 90,000 mel 帧预算另验证 1,716 条，1,534 条成功结果全部通过原生参考，最大绝对差 9.06e-6；其余均为无合法 reference。
- packaged libsndfile 的 BytesIO 与内存文件输入在 256 条真实样本上逐值一致；reference 使用内存文件输入，并严格核对 codec 原生长度。
- 自动回归 230 项通过；验证了实际 speaker/merged 发布、重复运行、空输出分片、失败过滤与来源表保留。

验收证据位于 `artifacts/speaker-reference-validation/`，新生产运行及最终质量报告位于
`artifacts/reference-runs/`，当前运行入口为 `reports/reference/active.json`。
完成全部新表发布和校验后才清理本次临时特征表；最小验收证据和正式回归测试继续保留。

## 每卡多 worker 调度

`run --workers-per-gpu N` 为每张 GPU 启动 N 个独立上下文，共用任务队列。
默认仍为 1；每个任务只由一个 worker 写入，采样计划、batch=64、数值内核与 profile 不变。
`--memory-fraction` 是每个 worker 的上限，`--decode-threads` 也是每个 worker 的线程数。
执行 manifest 记录这些运行参数。启动时只传递各数据集的必要元数据，任务明细通过队列发送，
避免为每个进程重复序列化整库任务目录。

2026-10-08 的 44,514 条真实读取、解码、推理与完整写回测试（GPU 6，正式任务同时运行）：
单 worker/16 解码线程两次为 303、292 条/秒；2 worker/每进程 8 线程为 394 条/秒；
3 worker/每进程 8 线程为 530 条/秒。44,514 条样本在四轮测试间的 embedding 最大差为 0，
输入波形摘要与状态一致。启动和最终数值对照不计入上述流式吞吐。
另对 6 个大数据集测试 batch=64/128/256，128/256 平均收益仅约 1%～3%，不采用实验内核。
多卡收益须以生产稳定区间测量为准，不能直接将单卡倍率外推。

改变 worker 数只更新执行代码和运行参数；已有 run 切换时先退出当前 coordinator，
固定迁移前计划和代码摘要，再更新执行快照与 stage owner，从已验证检查点恢复。
不修改已生成的 reference、embedding 或其 profile，不重算已完成任务。
