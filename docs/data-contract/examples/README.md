# v0.1 示例

所有文件都是说明性示例，不是完整生产数据或可直接执行的 recipe。

| 文件 | 内容 |
| --- | --- |
| base-row.example.json | 从 0.1 秒合成静音 WAV 生成的真实基础字段投影；只省略 binary |
| quality.example.json | 同一主表的 missing / failed / ok / 0 分区分 |
| annotation-manifest.example.json | 列内质量 run 的 manifest、snapshot 与覆盖统计 |
| view.example.json | 原生帧区间、父音频与 speaker 范围 |
| codec-profile.example.json | 必须填全并验证才能生成 profile_id 的模板 |
| codec-row.example.json | [time,codebook] 整数数组；K=2 仅用于说明 |
| training-recipe.example.yaml | 固定所有来源 snapshot、质量策略、codec 和采样的模板 |

manifest 的整数 snapshot 只是例子，不表示生产已提交。质量分数与 codes 是示意值，不能作为真实模型输出。
机器可读 Arrow 类型由 `scripts/export_contract.py` 生成，业务约束仍需验证器执行。
