# 尚未转写数据集的 Qwen ASR supervisor

用户最新授权：对所有已发布且尚未完成 transcription 的数据集做全量 Qwen3-ASR-1.7B 标注，
排除 Emilia2，并挂 supervisor 检查。范围已从 Emilia-YODAS 扩大；最新指令覆盖之前 Galgame 全量暂缓。
本说明是本轮的执行范围；不要沿用四个游戏旧 supervisor 的任务范围、run 或入口。

## 固定任务

- repo：`/workspace/workspace/yanglin/tts-data-pipeline`。
- 工作根 ROOT：`artifacts/asr-unannotated-20261010`。
- 计划：`ROOT/work/plan.json`；run_id、精确输入版本及 profile 从该文件读取。
- 范围以 `ROOT/scope.json` 和固定 plan 为准：13 个数据集，共 133,696,192 条。
  aishell3、csemotions、emilia、emilia_yodas、galgame、hifitts、hifitts2、libriheavy、
  libritts_r、ljspeech、mls_sidon、vctk、wenetspeech4tts。
- Emilia2 排除；已完成的 genshin_voice、starrail_voice、wutheringwaves、zenless_voice 不重复跑。
- FireRedLID 正式全量继续暂停；不要启动其 full-work。
- `artifacts/emilia-yodas-asr-20261010` 仅留本轮启动前的 YODAS 抽样验证证据；
  被替代的准备入口、代码副本和非正式计划已清理，不再有独立运行入口。
- Qwen 服务：`http://127.0.0.1:18101/v1`，模型名 `Qwen3-ASR-1.7B`，vLLM `0.18.0`，
  模型根 `/workspace/model/Qwen3-ASR-1.7B`。共享服务不得由 supervisor 重启或修改。

流程：固定 samples → 验证音频 SHA256 → 完整解码、声道均值、重采样至 16 kHz →
每条完整 FLOAT WAV 请求 → 自动语言主转写 → 来源语言冲突时另存指定语言候选 →
每 512 个目标提交带摘要的检查点 → 全量完成后发布独立 sample_table → 全字段回读、ID 索引及固定版本 →
最后发布全局 complete manifest。对外只有一张 results.lance，内部兼容双 Parquet 检查点。

不裁剪、不补波形；没有 FireRedLID 的 200 秒上限。原文不作提示，不被覆盖；不自动生成 selection。
主转写不强制来源语言，Qwen 支持的来源语言冲突候选与主结果分开保存，路由固定在 profile。
超时 600 秒；每请求临时错误重试 3 次、间隔 2/5 秒；耗尽后客户端等待 60 秒自动断点恢复。

## 启动与检查

只有 `ROOT/active-launch.json` 存在后，supervisor 才接管已经由主 agent 启动的正式任务。
在此之前等待主 agent 通知；不得把准备好的 plan 当成启动授权信号，或抢先重复启动。
第一次接管立即检查，之后每小时检查一次。所有时刻保存明确时区，用户报告用北京时间。

每轮读取 active-launch 的 command/env/PID，以及 work/status.json、supervisor.json、run.log 尾部、
最近检查点、failures.jsonl/recovery-events.jsonl 新增部分。确认 PID 命令匹配当前 work，不能仅靠 PID 存在。
`hourly-supervisor/checks.jsonl` 追加检查结论；`state.json` 原子记录本次、下次检查、进度、错误和采取的动作。
检查点提交量才是实际处理进度；恢复时重放旧检查点不能算新增吞吐。

| 状态 | 处理 |
| --- | --- |
| running 且持续提交检查点 | 记录，不重启 |
| recovering，重试记录持续更新 | 让内部自动恢复继续，不启动第二个客户端 |
| publishing | 检查发布日志和产物，不能以转写条数不增长判定卡死 |
| complete | 校验全局 manifest 真正存在、status=complete、13 个固定输入和输出逐一对账、共覆盖 133,696,192 条、missing=0；回报并结束监督 |
| 进程已退出，最后是临时网络/服务故障或证据充分的意外退出 | 确认无同 work 进程且锁已释放，检查服务模型/root/version后，用 active-launch 的固定 command/env 断点恢复 |
| 新 HTTP 400、协议异常、模型/源版本变化、身份冲突、检查点损坏、磁盘错误 | 保存证据并通知主 agent，停止自动重启；不跳过大批样本，不修改计划或权重校验 |
| 进程活着但暂时无进度 | 600 秒请求超时、3 次重试及批次收尾可能较久；先结合请求/CPU/日志判断，不能凭几分钟无提交就杀进程 |
| 用户暂停/取消 | 停止恢复，以用户最新指令为准 |

恢复使用 Python subprocess.Popen，stdin=DEVNULL，stdout/stderr 追加 ROOT/run.log，start_new_session=True。
先核对当前命令与锁，保存旧 launch/status/execution，再将新的 PID/command/env 写入
`ROOT/resumes/<UTC timestamp>/launch.json` 和 `ROOT/active-launch.json`。
不得匹配或终止所有 python/vllm 进程，不调整其他数据处理、audio_stats、训练或服务。
只能依据明确证据恢复本任务；需要代码修复时先报告主 agent，不自行修改已固定 profile。

## 记录与回报

逐条失败由客户端自动即时记录，不靠 supervisor 补写；正式终态以检查点及发布单表为准。
普通检查给主 agent 简短进度即可。异常、恢复动作、需外部干预及最终完成及时报告。
监督持续到本 run 完成或用户明确暂停/取消；不只做一次检查就结束。
