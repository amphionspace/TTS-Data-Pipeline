# 13 · 独立文本特征表

当前提取采用 codec、speaker_embedding、text 三类独立表。text 只物化 selection 已选定的
最终文本、语言与来源，不做 token 化、重新转写或额外清洗，也不依赖 codec 是否提取成功。

发布路径为 `datasets/<dataset_id>/<release_id>/features/text/<run_id>/{manifest.json,features.lance/}`。
run 命名沿用 feature 规则，kind=text，profile_name=selection-text-v1。每个 run 固定 selection
manifest 摘要、源表分支/版本、文本来源与处理实现；完整覆盖该 selection 中本数据集的目标。

## 列与身份

独立表包含 06 中的九个选用元数据列，以及非空 string 列 target_id、release_id、audio_sha256。
`target_id=sample_id`，不复制音频 bytes。关联键为 `(dataset_id, release_id, target_id)`；
codec/speaker 的 dataset_id、release_id 从其固定 manifest 取得，text 行中显式保存。
通过 audio_sha256 核验音频来源。模型 profile、feature_key 或物理行序均不是三表的关联键。
text 表不含音频 feature_key，也不声称其内容与任一音频 encoder 的输入波形摘要相同。

text/selected_text 和 language/selected_language 按非 null 覆盖语义选择。text 必须非空白，
但不在提取时额外 strip、改标点、转简繁或猜语言。text_revision 沿用 05/06 定义，
text_source 对应固定 selection.text_sources。当前执行器支持基础文本及 selection 已完成的规范化；
新 annotation 文本来源在实现其原文修订绑定之前必须报错，不得静默回退到基础文本。

## 执行与发布

直接复用已审计的音频目标计划；逐任务核对有序 target_id 摘要，读取文本列，不解码音频。
任务独立落盘、全量回读比对后保存文件摘要 checkpoint；恢复时验证文件摘要。
不符合文本约定时中止并保留已完成任务，不跳过目标或生成空白替代文本。
发布时核对组装后的全部目标顺序与固定任务计划、建立 target_id BTREE，固定 Lance 版本，
写 complete manifest 后将 `.incomplete` 目录原子改名。未发布目录不能用于训练。
成功发布的 coverage.ok 等于完整目标数；这不代表 codec 和 speaker 已全部成功。

## 三表合并边界

三表合并以 selection 为基准，按关联键检查唯一性、缺失、来源与状态；训练专用构建由训练端管理。
不直接 zip 不同行序的表，不使用 inner join 静默丢弃缺特征样本。
各 profile 的解码长度策略可能不同，合表时需核对其实际输入范围；不能仅因 ID 相同就忽略范围差异。
当前 codec 与 speaker 都按实际解码长度生成。历史严格头信息检查版本的成功结果可保留，
因为成功检查点已确认头信息与实际帧数一致；合表仍逐行检查实际区间。
text 不参与音频裁剪，也不负责掩盖该差异。

本轮已运行的 codec 仍按其冻结代码发布 text/language 列，暂不改动；独立 text 表不要求删除这些列。
将来合表以选定 text run 为文本来源，固定 run 和来源版本，并检查已有 codec 文本是否一致。
06 的随 codec 物化流程保留为兼容实现。三表独立发布并永久保留；合表另建输出，
不删除、覆盖或向来源表写列。
本契约只规定数据身份、内容和发布边界，不规定训练构建的目录、采样策略或读取布局。


## 合并表

入口为 `scripts/merge_features.py`，按 dataset/release 发布
`features/merged/<run_id>/{manifest.json,features.lance/}`。这是已提取特征的物化，
不做训练采样、token 化、音频裁剪或模型推理；不复制原始 audio bytes。
run 的 kind=merged、profile_name=selected-features-v1。

共同身份和文本列沿用独立 text schema，包含 dataset_id、release_id、target_id、audio_sha256。
codec 的其余音频特征列加 `codec_` 前缀，speaker 的其余列加 `speaker_` 前缀；例如
`codec_codes`、`codec_num_codec_frames`、`speaker_embedding`、`speaker_embedding_dim`。
两边的 profile_id、feature_key、输入摘要、区间和 status 均分别保留，完整 Arrow schema 由
来源 schema 加前缀生成，schema 摘要写入 manifest。codec 的九个文本元数据列先与独立 text
逐列核对，再仅保留 text 表的版本；不重复存储这九列。

固定三个来源的 manifest 路径/摘要、run、Lance 版本和 profile；三个来源须覆盖同一 selection
和目标集合。按 target_id 做一对一关联，缺失、重复、错误状态、音频哈希或选用文本不一致时停止。
连续区间读取仅是性能优化，必须核对 ID 集合并按 ID 重排；物理顺序不一致时按 ID 索引查询。

whole-sample 模式下 native_sample_rate 和 start_frame 必须一致，只接受 start_frame=0 的整条 sample。
实际 end_frame 相差不超过 20 毫秒（按原生采样率换算，至少容忍 2 个采样点）时，
保留各自原值并记入检查点，不补齐、不裁剪、不排除；
更大的差异报出 target_id 和两侧区间。编码器输入长度按各自重采样实现核验，不能要求不同
重采样算法的 encoder_input_sha256 相同。codec 帧数、码值范围及 speaker 向量维数/有限性也核验。

每块完整读回，对照输入合并结果逐列相等后写带文件 SHA256 的检查点；恢复时校验文件。
发布前，完整目标流再次核对固定 selection 的任务摘要，建立 target_id 索引、固定版本，
原子发布 complete manifest。manifest 的 inputs 保留三个来源和 selection 的基础快照引用，
validation 记录核对行数及微小区间差异数。原三张来源表继续作为独立产物保留。

## Reference merged 版本

`reference-features-v1` 复用完整 codec/text，关联新增的 reference speaker 表。
旧版的整条区间一致性规则仅适用于 whole-sample 模式；reference 模式要求 speaker 区间是
该 codec 实际音频的合法子区间，并严格验证 [11](11-speaker-embeddings.md) 的时间映射及 codec feature_key，
不能用 20 ms 整条时长容差替代 reference 定位检查。

列命名继续沿用前缀约定：`speaker_reference_codec_start/end` 直接定位 `codec_codes` 的时间维，
`speaker_start_frame/end_frame` 是原始音频采样点，`speaker_encoder_input_num_frames` 是裁后重采样输入长度。
完整文本和完整 codec 数组不做数值变换；尾帧仍保留。

三个来源表仍覆盖同一原 selection。合并前显式检查 speaker 状态，失败行不进入新 merged。
manifest 的 `reference_filtering` 记录输入总数、有效数及各排除原因，必须满足数量守恒。
新 merged 的 selection 使用 subset 模式，在本 run 保存 `targets.lance`，固定其快照、有效目标数及目标集合摘要，
同时保留原 selection manifest 和全部来源快照以供追溯。不以旧 merged payload 为输入。
原子发布新 run，不覆盖或删除旧 speaker、codec、text、merged，也不切换训练配置。
