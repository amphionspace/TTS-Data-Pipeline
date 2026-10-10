# CPU audio_stats

对固定、已发布的 samples 完整音频提取确定性波形统计，结果独立发布，不改变音频、字幕或训练资格。
字段和定义见 [annotation contract](data-contract/specs/05-annotations.md#独立音频统计结果)。

运行入口：

```bash
python scripts/annotate_audio_stats.py prepare \
  --root /workspace/data/DATA-TTS-UNIFIED --work artifacts/audio-stats-<date>/work
python scripts/annotate_audio_stats.py run --work artifacts/audio-stats-<date>/work --workers 32
```

prepare 枚举 complete 的 base manifest，验证其身份验收和行数，固定 snapshot、fragment 和逻辑行范围。
没有发布的 Emilia2 不纳入。小测可指定 `--sample-rows 64 --output-root artifacts/<pilot>/output`，
在每个数据集首、中、尾三个 fragment 的中间读取固定行子集；这不是总体质量无偏估计。

正式任务使用冻结源码，每 worker 流式读取 32 条音频，每个最多 8,192 行的任务保存独立检查点。
CPU 进程采用 spawn，Lance/BLAS 内部线程数应限制，避免与 ASR 服务争抢大量 CPU 和磁盘带宽。
统计不依赖 GPU。编码音频 SHA256 逐条核对；波形仅以短块解码，不因长音频一次分配完整浮点波形。
多声道保留，低能量判断不会因反相混音误判。失败结果单独记账，不生成假分数。

复用同一冻结代码执行 run 可恢复检查点；参数和依赖不能偷偷改变。变更指标定义需新 profile/run。
`work/status.json` 给出覆盖、失败、各数据集进度、已处理音频秒数和字节数。
正式产物为每个 release 下的单张 results.lance（sample_table），以及最后发布的全局 annotation manifest。
每个目标一行：sample_id/input_fingerprint/status/error_code/result；失败 result=null。
内部双 Parquet 检查点继续保留以兼容已有计算，不再分别发布 targets/results 两张 Lance 表。
原始表、现有特征和 ASR 输出不被覆盖；本任务也不直接筛选样本。

验证材料：`artifacts/audio-stats-pilot/` 和 `artifacts/audio-stats-bench32/`。

2026-10-09 首次全量：`tts-ann-audio_stats-20261009T193447bjt-01`，
17 个已发布数据集，135,239,378 条。冻结代码、启动信息和运行日志：
`artifacts/audio-stats-20261009/`；进度：`work/status.json`。
32 个 CPU worker，nice=10，BLAS/Lance CPU 内部线程限制为 1。

前置验证：3,264 条跨数据集试跑、13,056 条扩展试跑均成功，均完成独立结果表发布回读。
51 条真实音频单条复算与 worker 结果一致；独立整段解码对照的最大 RMS 绝对差约 5.55e-11，
观察到一个 MP3 峰值差约 1.19e-7，来自两种解码读取方式；统计生产固定为同一分块方式。
32-worker 小测包含 93,740 秒音频、4.23 GB 编码数据，处理阶段 63.48 秒，含发布共 78.32 秒。
样本跨数据集等量抽取，不能直接用总条数除小测吞吐预测全库；按各数据集样本耗时加权，
初步约 35 小时，不含大规模索引与最终发布的不确定耗时。正式持续吞吐优先于小测估算。

## 单表发布（audio-stats-v2）

2026-10-10 按用户要求将本轮尚未发布的全量任务改为单表输出。
计算函数和旧检查点格式保持不变，发布时逐检查点核对成功行的 ID/输入指纹，
将状态和结果合成一行；失败 result 为空，不填零、不删除该目标。
单表按 sample_id 建索引，完整回读后再发布；manifest 使用 storage_kind=sample_table 和单个 table 引用。
内部仍保留双 Parquet 检查点，避免重写已完成数千万条记录；这不属于对外发布格式。

成功行示意（数值取自一条已处理 Zenless 音频，省略哈希和部分统计字段）：

```json
{
  "sample_id": "0dd746ed5a0d37e7425f22d2ab636aac779262672c200b70e7da5acde4881778",
  "input_fingerprint": "438394543b77f06d058b297eb401a7bdd9448161e923d3094bf7c59cef10a0dc",
  "status": "ok",
  "error_code": null,
  "result": {
    "native_sample_rate": 48000,
    "channels": 1,
    "decoded_num_samples": 79237,
    "duration_seconds": 1.6507708333333333,
    "peak": 0.67596435546875,
    "rms": 0.10149017443074057,
    "near_full_scale_ratio": 0.0,
    "low_energy_ratio": 0.15191135454396304,
    "low_energy_intervals": [
      {"start_sample": 0, "end_sample": 1920},
      {"start_sample": 69120, "end_sample": 79237}
    ]
  }
}
```

失败行仍有 sample_id/input_fingerprint，status=failed、error_code=audio_nonfinite 等、result=null。
result 中另保留音频 SHA256、原生区间、声明格式/长度及差值、各声道统计、满幅与零值比例、首尾/最长低能量长度。
低能量是约 20 ms 窗口所有声道 RMS ≤ -50 dBFS 的统计证据，不是 VAD 或质量评分。

本轮正式恢复入口：`artifacts/audio-stats-20261009/code-single-table-v2/scripts/annotate_audio_stats.py`，
具体命令/PID 以 `active-launch.json` 为准。`work/implementation-migration.json` 固定旧/新实现、
计算代码摘要、检查点 schema 和验证报告。完整 17 数据集的 3,264 条转换发布回读通过，11 项测试通过。
全量仍在执行，完整单表在全部目标完成后发布；本次验证表仅在 artifacts 中。
