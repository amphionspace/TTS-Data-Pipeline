# ACP 提交

一个 ACP 任务在一台 8 卡节点上跑完某个 selection 的全部特征（codec、text、reference speaker、merged），
调度逻辑见 `scripts/feature_pipeline.py`：

```bash
python scripts/feature_pipeline.py init --work-root <dir> --selection <selection manifest> \
  --codec-acceptance <codec acceptance.json> --speaker-acceptance <speaker acceptance.json> \
  --late-datasets emilia2
python -m scripts.acp.submit --work <dir> --name <job> [--submit] [-- <feature_pipeline.py run 选项>]
python -m scripts.acp.status --work <dir>
```

`init` 固定输入，并找出可复用的已发布 codec / speaker 结果；`submit` 默认只渲染请求，加 `--submit` 才创建任务。
平台参数在 `configs/acp.yaml`，凭据在仓库外的 `/workspace/workspace/yanglin/acp/.credentials.json`，
与训练仓库共用平台账号，代码互相独立。

ACP 节点只挂载 `/workspace`，工作目录、模型缓存和 Python 环境
（`/workspace/workspace/yanglin/envs/tts-features`）都必须在 `/workspace` 下。
提交时把 `src`、`scripts`、`configs` 冻结到 `<work>/submissions/<UTC 时间>/runtime`，任务从快照运行，
请求与返回的 job 信息保存在同一目录。容器以 root 启动，`pipeline.sh` 先切换为工作目录所有者再运行，
输出文件因此归该用户所有；每分钟把 GPU 利用率、显存和 CPU 负载写入 `<work>/acp-monitor/`。
中断后重新提交同一工作目录即可：各步骤从检查点继续。
