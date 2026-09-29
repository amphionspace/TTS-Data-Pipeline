# Codec padding 与批处理约束

有效长度裁剪输出不能消除 encoder 内部 padding 的影响。混长输入在多层 strided CNN 中
会把前层无效位置带入后层有效输出；downsample 的 replicate padding 也必须按单条有效末尾计算。
此外 GEMM 的批形状变化可能造成浮点差异并跨过最近邻量化的决策边界。

正式 C 的处理：

1. 对每层卷积维护有效长度，需要时屏蔽无效位置；downsample 复制最后一个有效点。
2. FA2 固定全因果注意力，避免默认局部窗口与原始 SDPA 基线的语义差异。
3. 单条长度确定 2 秒 bucket 和固定批大小；任务尾部仍补足空槽，不能根据 GPU 空闲量改形状。
4. 显式固定试听版 FP32 码本缓存，避免模型预热顺序改变后续 FP16 的结果。
5. 8 卡同时测试换序、换邻居、换槽位、单条、尾批以及最长 120 秒边界。

C 允许和旧 FP32 A/B 不同；同一 C profile 内重试和调整运行并发要求整数 codes 精确一致。
数值规则见 [06 contract](../data-contract/specs/06-codecs.md)，实际证据见
[codec 验收](codec-inference-validation.md)。
