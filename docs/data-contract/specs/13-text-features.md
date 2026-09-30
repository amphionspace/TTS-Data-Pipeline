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
当前 codec 严格头信息检查与 speaker 实际解码长度的差异，留待合表前核查和必要补算。
text 不参与音频裁剪，也不负责掩盖该差异。

本轮已运行的 codec 仍按其冻结代码发布 text/language 列，暂不改动；独立 text 表不要求删除这些列。
将来合表以选定 text run 为文本来源，固定 run 和来源版本，并检查已有 codec 文本是否一致。
06 的随 codec 物化流程保留为兼容实现。当前三表独立发布，后续合表需另行实现与验收。
本契约只规定数据身份、内容和发布边界，不规定训练构建的目录、采样策略或读取布局。
