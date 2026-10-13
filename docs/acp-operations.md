# ACP 运维：共享文件系统丢失 rename 时如何跑完 feature 任务

本文记录 2026-10-12 selection-v2 feature 任务（`artifacts/feature-runs/selection-v2-20261012`）中遇到的问题、
pipeline 里的应对和运维工具。提交与状态查询见 [scripts/acp/README.md](../scripts/acp/README.md)。

## 1. 问题：ACP 节点上的 quarkfs 不能可靠地 rename

`/workspace` 是 quarkfs FUSE 挂载。在 ACP 节点上（dev host 上同一文件系统没有出现过），rename(2) 有两种失败：

- **静默丢失**：rename 返回成功，写入方随即 stat 和读回校验都通过，几分钟后目标文件消失，源 `.tmpXXXX`
  原样留在原处（Lance 数据文件、`write_json` 写的检查点 JSON、speaker 参考计划 `.arrow` 都出现过）。
- **报错**：Lance 持久化临时文件时返回 `Invalid argument (os error 22)`；Triton JIT 缓存 put 返回 `ENOTEMPTY`。

2026-10-12 夜里约 30 次静默丢失、数次 EINVAL，既有集中爆发（23:42–23:50 PDT 22 次），也有零散发生。
Lance 写数据文件、所有 `write_json` 都是"写临时文件再 rename"，所以任何步骤都会受影响；
最敏感的是 codec 发布：`add_columns` 为每个分片新写一个文本列文件，全部成功才算发布成功
（emilia 约 1 万个、emilia2 约 3.3 万个文件），任何一个丢失都让整次发布失败、step 重启。

## 2. pipeline 内的应对（已在 main）

| 机制 | 位置 | 作用 |
| --- | --- | --- |
| 丢失即重做 | `feature_runtime.lost_file`（`verify_checkpoint` 调用） | 检查点引用的数据文件不存在时，把任务当作未完成重做；若 stage 已有失败的提交先丢弃（只在拥有者的 `.incomplete` stage 内）。丢失超过 max(8, 1%) 视为挂载问题直接报错。每次记录在 `state/lost_files.jsonl` |
| 重试与进度 | `scripts/feature_pipeline.py` `Step` | 每个 step 8 次重启；运行超过 30 分钟，或本次启动后 rows_done 前进或发布了数据集，则崩溃不消耗重启次数；有进度仍崩溃 20 次以上在日志里标 ALERT |
| JIT 缓存在节点本地 | `scripts/acp/pipeline.sh` | Triton/numba/CUDA 缓存放 `/tmp`，避免 ENOTEMPTY |
| 启动暂停 | `pipeline.sh`：存在 `<work>/hold` 时等待 | 让重新提交的任务排队，同时在别处做完会冲突的操作；遗留的 `hold` 会让任务无声等待 |
| 节点只编码 | `feature_pipeline.py run --codec-mode encode` | 节点只跑 GPU 编码任务，复制和发布由另一台机器 `extract_codec.py run --mode reuse` 完成；codec 启动前若有数据集"任务全完成但未发布"则等待（`<work>/codec-gate.json`），避免两处同时发布同一 stage。共享 dev host 繁忙时发布很慢，本次未采用 |
| speaker-early 并行 | `feature_pipeline.py` | 早期数据集的 codec 发布后即开始 speaker/merge，与晚期数据集编码并行 |

## 3. 运维工具

**丢失文件恢复**（在 rename 可靠的机器上运行，与任务并行）：

```bash
python scripts/repair_lost_files.py --work <work> --apply --every 120 --quarantine <work>/quarantine
```

用大小和 sha256 都与检查点记录一致、且存在超过 2 分钟的源恢复丢失的名字（数据文件、检查点 JSON、参考计划）；
不覆盖、不删除；跳过已在 `lost_files.jsonl` 中重做的任务；`--quarantine` 把旧 `.tmp` 硬链接出 stage，
防止 codec 发布清理删掉唯一副本。还会报告 stage 最新提交或已发布表引用了不存在的文件。日志 `<work>/repair.log`。

**自动重新提交**（在主 checkout 下后台运行；重新提交会快照主 checkout 的工作区）：

```bash
setsid nohup scripts/acp/supervise.sh <work> <job-name> --merge-workers 12 > /dev/null 2>&1 &
```

任务 FAILED 时重新提交，最多 3 次；连续两次 20 分钟内失败、`src`/`scripts`/`configs` 有未提交改动、
任务被人工停止/挂起/删除时不提交。日志 `<work>/supervisor.log`。

**发布后审计**（只读）：

```bash
python scripts/audit_features.py --work <work> --threads 32 --require-all --out <work>/audit/audit.json
```

逐个已发布 final 检查 manifest、版本与标签、全部引用文件、全量读取每一行、索引、输入 manifest 摘要、
merged 账目（= text − codec/speaker 非 ok 行）与 targets.lance、跨步骤行数。selection-v2 规模（约 3.8 TB）约 30–60 分钟。

**任务监控**（只读，出事才写 WAKE）：

```bash
setsid nohup python scripts/acp/overseer.py --work <work> --watch repair_lost_files.py supervise.sh > /dev/null 2>&1 &
tail -F <work>/overseer.log | grep --line-buffered ' WAKE '
```

任务状态不再是 QUEUEING/STARTING/RUNNING（含 SUCCEEDED）、`logs/*.log` 新出现失败行
（Traceback、`"phase": "failed"`、`publish_failed`、`# exit code`、ALERT）、被看守的进程不在、
某个 step 的 rows_done 长时间不动（运行中且没有 `hold`）、容器内存过高时各写一条 WAKE，同类去重。

**dev host 发布（最后手段）**：

```bash
python scripts/devhost_publish.py --work <work> --step codec --dataset <dataset_id>
```

用任务最新快照的固定代码（校验代码摘要）在本机完成一个 codec/text 数据集的发布并核验结果。
任务处于 QUEUEING/STARTING/RUNNING/SUSPENDED 时拒绝执行，并在 work root 加排他锁。
只在节点上同一数据集反复发布失败、且 dev host 负载不高时使用。

## 4. 操作要点

- 停止任务用 API 删除（`DELETE trainingJobs/<name>`）并确认 404，不要只挂起：挂起的任务仍可能占用唯一节点配额或被恢复。
- 两处同时对一个 stage 做 finalize 会互相删除对方的文件。要在 dev host 上发布某个数据集，必须先删除任务、确认节点上没有进程。
- 共享 dev host 可能被他人占满（2026-10-12 负载 120+，本地发布 emilia 每分钟约 22 个文件）；把重活放到 dev host 前先看负载。
- 已发布 final 的旧版本按 [12](data-contract/specs/12-selections.md) 默认不回收；2026-10-13 的一次性例外记录在
  [PUBLICATIONS](data-contract/PUBLICATIONS.md)。
