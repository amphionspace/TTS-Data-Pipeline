# v0.1 示例

所有文件都是说明性示例，不是完整生产数据或可直接执行的 recipe。

| 文件 | 内容 |
| --- | --- |
| base-row.example.json | 从 0.1 秒合成静音 WAV 生成的真实基础字段投影；只省略 binary |
| quality.example.json | 任务分支的未选/failed/ok/真实0分区分 |
| annotation-manifest.example.json | 平级annotation分支、固定bv、子集覆盖与对齐验证 |
| text-revision.example.json | 未处理/keep/replace/failed与selection稀疏覆盖，不以null清空文本 |
| view.example.json | 原生帧区间、父音频与 speaker 范围 |
| codec-profile.example.json | 未验收的 FP32 格式模板，不代表当前生产 C；只对 profile 对象生成 ID |
| codec-text.example.json | selection 选用文本/语言物化到 codec 表，文本身份与音频身份分开 |
| codec-row.example.json | [time,codebook] 整数数组；K=2 仅用于说明 |
| speaker-profile.example.json | 冻结 ECAPA 格式模板；此文件的占位权重/前处理不能用于生产 |
| speaker-row.example.json | D=3 的合成向量存储示例 |
| feature-manifest.example.json | codec run 的固定输入/快照/完整终态记账示例 |
| feature-subset-manifest.example.json | 一个父 sample、100 个 views 中选择两个的 run；绑定 targets 表 |
| feature-targets.example.json | 上述子集的真实合成 ID 和目标集合摘要输入 |
| training-modes.example.json | 冻结/在线、speaker-only/ICL 和 self 的条件字段投影 |
| selection-rules.example.json | 合成训练规则、原因优先级、语言alias；时长阈值仅为例子 |
| selection-manifest.example.json | 两个合成数据集组成的完整 selection 元数据，不对应生产分支 |
| training-plan.example.yaml | 独立于 build 的采样、评估与恢复模板 |
| training-recipe.example.yaml | 固定 selection、特征绑定及 self 协议的数据 recipe；不含采样权重 |

manifest 的整数 snapshot 只是例子，不表示生产已提交。质量分数与 codes 是示意值，不能作为真实模型输出。
机器可读 Arrow 类型由 `scripts/export_contract.py` 生成，业务约束仍需验证器执行。
