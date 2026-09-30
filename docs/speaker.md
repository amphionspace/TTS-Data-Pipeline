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
