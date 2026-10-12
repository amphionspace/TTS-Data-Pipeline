# CPU audio_stats

对固定、已发布 samples 的完整音频提取确定性波形统计，结果独立发布，不改变音频、字幕或训练资格。
字段、计算口径和失败语义见 [正式 05 · 独立音频统计结果](data-contract/specs/05-annotations.md#独立音频统计结果)
（`audio-stats-v1`，profile `mono-mix-waveform-stats-v1`）。代码位于 `src/tts_data_pipeline/annotations/audio_stats/`。

要点：整段 `soundfile.read` 解码，与 codec 相同的 float32 声道均值，原生采样率，float64 累计；
记录实际帧数与 base `num_frames` 之差及解码期间的 stderr 警告行数。统计只是测量证据，筛选由 selection 决定。

## 运行

```bash
python scripts/annotate_audio_stats.py prepare \
  --root /workspace/data/DATA-TTS-UNIFIED --work artifacts/audio-stats-<date>/work
python scripts/annotate_audio_stats.py run --work artifacts/audio-stats-<date>/work --workers 96
```

prepare 枚举 complete 的 base manifest，验证其身份验收和行数，固定 snapshot、fragment 和逻辑行范围，
同时固定代码摘要和依赖版本。未发布的数据（如 Emilia2）不纳入，发布后另起 run。
小测可加 `--sample-rows 64 --output-root artifacts/<pilot>/output`：每个数据集首、中、尾三个 fragment
各取一段固定行；这不是总体质量的无偏估计，输出也不写入统一根目录。

每个任务最多 8,192 行，worker 流式读取 32 条音频；CPU 进程采用 spawn，Arrow 内部线程限制为 1。
全库最长音频约 740 秒（galgame），整段解码的单条内存在 0.2 GB 以内。

## 恢复与输出

用同一 work 再次执行 run 即恢复：已完成检查点按任务、计划摘要和文件摘要核对后复用。
代码摘要、依赖或 libsndfile 版本与计划不一致时拒绝运行；生产任务应从冻结代码副本启动。
变更任何指标定义需新 profile 和新 run。

- 进度：`work/status.json`（覆盖、状态计数、错误码、各数据集行数、音频秒数/字节数）。
- 检查点：`work/checkpoints/<task_id>/results.parquet`，与发布 schema 相同。
- 发布：每个 release 下 `annotations/audio_stats/<run_id>/results.lance`（sample_table，含失败行），
  以及全局 `annotations/audio_stats/<run_id>/manifest.json`。

当前状态：执行器已按 audio-stats-v1 重写，尚未完成全量运行。
