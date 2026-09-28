# 08 · 来源接入、验收与失败恢复

## Adapter

一个 dataset 一个 adapter 文件，共享容器读取、身份、Lance writer 和验证器。
adapter 固定 canonical dataset_id、来源单元、source_key、字段映射、语言/speaker 范围、文本类型和原 split/config。
来源清单在开始前固定；运行中新增加的文件不自动进入已固定计划。来源原目录只读。

| 来源 | 规则 |
| --- | --- |
| HF Parquet | 保留 bytes；测量真实音频头；源 schema 的采样率声明不是测量结果 |
| tar 家族 | 完整路径键配对音频/标注；校验重复、缺项和 EOF，不依赖相邻顺序 |
| tar+idx | 先核对归属 archive、偏移、长度、成员名；旧编码索引不能指向新 bytes |
| ZIP/7z | 检查清单和配对；提取到受控临时区，不回写原目录 |
| 音频目录+manifest | 固定全部语义依赖；双向检查音频/标注缺失 |
| 长录音 | 基础存载体，view 存片段，转写缺失保留 null |
| 噪声/RIR | 独立 assets 表与资产 schema，不伪装成监督 TTS 样本 |

## 基础发布门槛

1. 来源数量、大小和完整 SHA256；有上游 sidecar 时核对，parquet footer 行数或 archive 完整配对计数对账。
2. 逐行 schema、必需值、sample_id、record_revision、文本类型、speaker 范围与 JSON 合法性。
3. 写出后全量回读：音频 bytes 哈希、音频头、记录指纹与来源顺序摘要一致。
4. 跨 worker 的全局 sample_id 唯一性、总行数、配置/来源键交集检查。
5. 提交 Lance 表，核对计数、索引覆盖和固定 snapshot 读取；记录文件哈希及验证覆盖。
6. 写 complete manifest，再发布目录。普通读取入口不能读取 incomplete。

standard 校验音频头和 bytes，不声称已完整解码全部波形；deep 额外完整解码、检查有限值/帧数并复核来源哈希。
两者都不代替听检、音文一致性和 speaker 真实性检查。抽样结果不得写成整库验收。
发布默认 fail-closed：一条未预先排除的结构错误即失败。
允许经确认的来源排除清单：固定 source 相对路径、大小、完整 SHA256 与原因，先对账原始清单，再明确排除。
计划和发布 manifest 保存 excluded_source_files；发布范围注明排除，不声称覆盖上游全部文件。
被排除文件内容变化必须重新核对规则；恢复要求排除策略相同。不能据此自动忽略其他未知坏包。
逐记录排除与整文件排除分开配置。启用前必须固定显式配方与逐条拒收明细。
当前支持对 Galgame / LibriHeavy 已逐条核实的 Parquet 零帧、容器格式错误或无法识别的音频作精确排除：记录源相对路径、源文件 bytes/SHA256、原始零起始行号、
上游 ID（Galgame 为 audio_ID，LibriHeavy 为 id）、音频 bytes/SHA256、condition 和 reason。
必须核验源文件与记录身份。zero_decoded_frames 要求读取器报告 0 帧、实际读取也返回 0 帧；
sndfile_malformed 要求当前固定版本读取器在打开音频时报告 SF_ERR_MALFORMED_FILE (3)；
sndfile_unrecognised 要求报告 SF_ERR_UNRECOGNISED_FORMAT (1)，且该条记录已经人工核实并列入内容固定的清单。
其他异常、目标恢复可读或身份不匹配均失败。这两种错误条件只声明当前读取器拒收，不声称其他解码器也无法恢复音频。
不得重编号原始行号；未排除样本保持原 ID。manifest 保存 excluded_source_records 与 rejected_rows，
输出行数 + 明确排除数 = 源 Parquet footer 行数；查不到排除目标或出现未知错误都不能发布。
排除清单纳入固定计划，恢复要求完全相同；不得把空音频修复为伪造波形或自动排除整文件。

## 并发、检查点与恢复

worker 生成 Lance fragment，逐文件验证后提交 checkpoint；单一协调者收集 fragment metadata 并提交主表。
文件名由引擎生成，不暴露调度 batch 作为公开目录。内部 checkpoint 可按 batch 保存。
恢复必须匹配来源、身份规则、存储格式、代码和影响结果的参数；worker 数可调整，样本身份不变。
复用前校验已完成文件哈希。未提交任务重跑，只有所有 worker 结束后，才能清理未被任何有效 checkpoint 引用的孤立文件。
禁止在并行写入时按目录差集删除其他任务可能尚未提交的 fragment。

重新 finalization 允许重建尚未发布的表元数据；已经发布的 release 不使用 overwrite 重跑。
state/checkpoint 是恢复工具，最终 manifest 不能依赖已删除的临时 JSON 才能识别自己的输入与 snapshot。

## 性能验收

记录源文件范围、冷/热缓存、worker 数、实际线程数、输入/输出字节、行数、小时、wall time、峰值内存和校验级别。
分别记录来源哈希、转换、回读、全局唯一性、索引与发布耗时。小样本速度不能外推整库吞吐保证。
采用有界 Arrow 批次与 backpressure，禁止把整个来源或整库音频变成 Python list。

## 显式修复迁移

默认禁止代码变化后复用检查点。修复确实不改变已完成批次的有效记录时，可进行单次显式迁移：
停止该任务全部 writer，保存旧计划，固定允许的完整旧/新代码哈希集合、修复原因及一致性验证证据，
检查旧完成批次不涉及新排除规则且文件哈希仍匹配，然后更新计划的执行代码版本。
恢复仍逐片校验，不能改写旧 checkpoint 的 code_sha256 假装由新代码生成。
发布 manifest 同时保存协调代码 code_sha256、checkpoint_code_versions、每批 code_version 与 code_migration。
未列入允许集合的代码、不同依赖版本或不同来源/排除范围必须拒绝。
此过程不是常规 resume 的自动降级；未经验证的语义变化应重建相应批次或使用新发布。
