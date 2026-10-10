# 四个游戏的 FireRedLID 语言标注

本任务从固定的已发布 samples 读取音频，新增 `spoken_language` annotation。
范围是 genshin_voice、starrail_voice、wutheringwaves、zenless_voice，共 1,543,186 条。
来源语言仍在 samples，ASR 语言仍在 transcription；本任务不覆盖它们，也不改变 selection。

字段、状态和发布约定见 [contract 05](data-contract/specs/05-annotations.md#fireredlid-音频语言单表)。
实现集中在 `src/tts_data_pipeline/annotations/firered_lid/`，入口为
`scripts/annotate_spoken_language.py`。本次模型目录为 `/workspace/model/FireRedLID`。

## 音频范围和模型

不做 30 秒切分。最多取原生音频前 200 秒，记录完整解码长度、实际分析区间和
`analysis_truncated`，不能把截断后的语言结论解释为已经检查完整尾部。
少量头信息长度差异按实际解码处理；原音频不变。

使用 [FireRedASR2S 的官方 FireRedLID](https://github.com/FireRedTeam/FireRedASR2S)
FP32 实现，beam=3、decode_max_len=2、softmax_smoothing=1.25、length_penalty=0.6。
关闭 TF32；不引入波形补零，不使用来源语言/ASR 语言提示模型。
前处理是声道均值、SciPy 重采样到 16 kHz、幅值乘 32768、官方 fbank/CMVN。
批处理按有效特征长度分桶，使用官方 mask；与单条模型结果做数值对照。

结果是整段语言证据，不是逐词语言定位，也不证明存在有效语音。
低置信度、来源冲突以及 `other`（规范化为 `und`）不自动判 failed。
权重、词表、CMVN、官方代码和实际运行依赖由 profile 固定。

## 执行、恢复与读取

缓存中隔离安装 kaldiio 2.18.0、kaldi-native-fbank 1.15、setuptools 80.9.0，
不升级正在运行的其他任务所用环境。官方代码放在 `cache/FireRedASR2S`，不复制整套模型代码到 src。

```bash
export PYTHONPATH="$PWD/cache/firered-lid-deps"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
python scripts/annotate_spoken_language.py prepare \
  --root /workspace/data/DATA-TTS-UNIFIED \
  --model-dir /workspace/model/FireRedLID \
  --upstream-code cache/FireRedASR2S \
  --work /path/to/new-work
python scripts/annotate_spoken_language.py run \
  --work /path/to/new-work --gpus 2,3,6 \
  --batch-size 32 --frame-budget 32000 --cpu-threads 8
```

示例 GPU 配置不表示这些卡一直空闲；实际并发按试跑及启动时资源确定。
`--frame-budget` 是 batch 的最大特征帧数乘 batch 行数预算，不是音频裁剪参数。
大于单 batch 预算的单条长音频仍完整处理到 200 秒上限。

每个 GPU worker 常驻一个模型，CPU 线程负责读取/解码/重采样和特征提取。
每 1,024 条以内形成私有检查点，包含成功和失败，经过完整回读后提交摘要。
`work/status.json` 保存进度，`work/checkpoints/*/checkpoint.json` 保存各段计数、错误原因和耗时。
中断后用同一个 run 命令恢复；输入、profile、模型、代码或检查点摘要不匹配则停止。
OOM 先减小 batch；单条仍 OOM、模型/身份冲突等停止排查，不静默跳过。

完成后每个 dataset/release 只有一张 `annotations/spoken_language/<run_id>/results.lance`，
成功失败各占一行，不另建 targets 表。各表全字段回读并建立 ID 索引和版本 tag 后，
才发布 `annotations/spoken_language/<run_id>/manifest.json`。读取者以此 complete manifest 为入口，
通过 sample_id 关联固定版本的 samples 和其他 annotation；不要直接消费私有检查点。

## 本次验证材料

工作目录：`artifacts/firered-lid-games-20261010/`。
`pilot-audio.parquet` 固定抽样音频，覆盖四个游戏、来源中英日韩、未知来源语言、极短及长音频边界。
`pilot-results.json` 保存单条与不同 batch 配置的原始结果和计时。
`throughput-work/` 和 `throughput-output/` 用于 3,072 条真实数据的独立端到端试跑。
这些属于试验产物，不是生产 annotation；最终运行参数和吞吐以验证报告与 execution 为准。
