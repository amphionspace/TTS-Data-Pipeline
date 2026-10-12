# ACP 提交

在仓库根目录运行 `python -m scripts.acp.submit --work <codec 工作目录> --name <任务名>`：
默认只渲染请求，加 `--submit` 才创建任务。平台参数在 `configs/acp.yaml`，凭据在仓库外的
`/workspace/workspace/yanglin/acp/.credentials.json`，与训练仓库共用同一平台账号，但代码互相独立。

ACP 节点只挂载 `/workspace`，所以工作目录、模型缓存和 Python 环境
（`/workspace/workspace/yanglin/envs/tts-features`）都必须在 `/workspace` 下。
提交时把 `src`、`scripts`、`configs` 冻结到 `<work>/submissions/<UTC 时间>/runtime`，
任务从快照运行；请求与返回的 job 信息保存在同一目录。

节点上运行 `scripts/acp/codec.sh`（默认 `--mode all`：复用与编码都在节点上完成；`--mode encode` 只做编码，配合本地 `--mode reuse`）：解码线程默认按节点核数自动分配，每分钟把 GPU 利用率、显存和 CPU 负载写入 `<work>/acp-monitor/`；用可见的全部 GPU 执行 `extract_codec.py run --mode encode`，
只处理需要编码的任务；可复用任务由本地 `--mode reuse` 在 CPU 上处理。两种模式使用各自的锁，
互不处理对方的任务；最后完成的一方发布对应数据集。
