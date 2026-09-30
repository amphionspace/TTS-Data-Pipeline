# Text 提取

当前 text 任务只物化 selection 的最终文本和语言，不做 token 化、不调用模型、不解码音频。
codec、speaker、text 使用相同固定目标计划，输出独立 Lance 表；完整字段和合表规则见
[data contract 13](data-contract/specs/13-text-features.md)。

```bash
python scripts/extract_text.py plan \
  --targets-plan artifacts/codec-runs/<codec-work>/plan.json \
  --work artifacts/text-runs/<text-work>
python scripts/extract_text.py run --work artifacts/text-runs/<text-work> --workers 2
```

默认输出到源 unified 根目录的每个 dataset/release/features/text/run；
验证时用 `--output-root artifacts/text-validation/output`，可用 `--datasets` 缩小数据集范围。
`run --max-tasks-per-dataset 1` 可验证有界执行，去掉限制后恢复。每个 dataset 一名 CPU worker，
默认最多同时两个；每次只读一个源 fragment 的文本/身份列，避免影响现有 GPU 提取。
断点以文本 fragment 回读和文件摘要验证为准；发布完成后重跑只校验已发布 manifest，不重复提取。

工作目录保存 plan.json、status.json 和 progress/<dataset>.json。正式运行应复制当前 src/scripts
到 work/runtime，并使用该冻结副本续跑；运行中不更改其代码或计划。
文本 checkpoint 存在 dataset 的 .state/release/features/text/run/checkpoints 下，
不在 src 中保存任务数据。最终合表尚未执行，文本表不能单独证明音频特征覆盖完整。

## 本轮验证

`artifacts/text-validation/summary.json`：16 个数据集各前最多 8 个任务，共 336,547 行，
所有文本、语言、修订、来源和身份字段逐条与固定 selection 独立核对，一致。
两名 CPU worker 物化及任务回读用时 20.67 秒，约 16,279 行/秒；按 128,220,178 行线性
外推约 2.19 小时，不含大表最终索引/发布，且前部任务不能代表全部数据分布。
CSEMOTIONS 4,160 行已通过部分执行、断点恢复发布、重复运行不重算验证。
相关自动测试 44 项通过，涵盖现有 codec 文本流程与新增 text 流程。
