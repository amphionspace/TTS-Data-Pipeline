# 07 · 训练构建、采样与读取

## 固定输入，离线构建

build_id = SHA256(canonical_json(不含 build_id/输出统计/时间戳的完整规范 recipe))。
recipe 引用 base、标注、view、feature 的 manifest 哈希和每张表的整数 snapshot；禁止 latest。
构建前验证引用和输入指纹，选择文本、speaker 映射、去重组、质量 run 与 codec profile。
长音频先选择或生成 view；同一音频多种视图不能未经处理重复增加采样权重。

训练 build 位于 `builds/<build_id>/`，包含 recipe.json、manifest.json、records.lance。
manifest complete 才可训练。base manifest 固定旧快照，而标注在较新快照时，build 指定包含所选列的
主表 snapshot，并验证它仍保留原基础键、revision 和音频；不能只从旧基础 snapshot 读取后来新增的列。

## 两种读取布局

默认 `materialized_codes`：训练表内嵌选定的 target codes、text tokens、协议实际需要的参考 codes/文本、长度、条件和权重。
冻结 speaker encoder 时同时内嵌选定 reference embedding；在线 speaker encoder 时保留固定快照的参考音频定位。
这会复制选定的 codec 数据，但不复制原音频，避免训练每条样本做多表关联。
适合先获得稳定吞吐。recipe 包含采样策略，修改策略仍产生新的 build_id；不得修改旧 recipe 后沿用旧 ID。
可通过引擎支持的数据文件复用节省复制，但新表仍须完整暴露内嵌 codes 列并保留依赖文件；未经验证先复制 codes。
新的训练记录、权重、example_id 和 manifest 均属于新 build；改用外部表定位读取则必须声明下面的 indexed_references。

可选 `indexed_references`：训练表存目标/参考业务 ID、实际使用特征的 feature_key、表引用索引和准确 snapshot 定位。
读取器必须按批量请求、局部性和缓存取特征，且基准满足训练吞吐后采用；跨 snapshot 不可复用行位置。
一个 build 只能选择一种明确布局，不能半数记录隐式依赖不存在的外部文件。

## 记录与配方

每行至少包含 example_id、target_kind/id/revision、选定文本及修订、language/speaker 条件、
目标 codec feature_key/摘要、有效 token 长度、sampling_weight、来源和 build_id。
参考身份、父 sample、固定表快照始终保留；参考特征 profile/key/摘要仅在实际使用该特征时必填，条件矩阵见 11。
materialized_codes 增加 codes/text_tokens/reference 列；具体 Arrow schema 由模型协议固定并保存在 manifest。
example_id = SHA256(canonical_json([build_id, target_kind, target_id, ordered_reference_ids, variant_key]))。
不得从物理行号推导 example_id；重复抽样可以重复取同一 example，不需要复制多行伪造身份。

recipe 必须固定：

- 来源和原始 split/config 选择；文本清洗、ASR/人工版本、语言与 speaker 映射。
- 去重、view 选择、硬过滤、每项质量状态的处理与回退。
- codec/text tokenizer/speaker feature 或 frontend profile；speaker_conditioning_mode、参考策略；特殊 token、最大上下文、padding、loss mask。
- 参考个数、选取方式、同 speaker 证据、同片段/重叠是否允许，参考文本是否使用。
- sampling unit（utterance/duration）、来源/语言权重、质量映射、归一化范围、seed、epoch 样本或 token 预算。
- train/eval 分组、分布式 world size/rank 分配与恢复规则。

## 音色克隆和评估

默认参考与目标来自已确认的同 speaker 的不同片段，排除同话语重复编码和时间重叠。
未知 speaker 不能靠同 dataset/group 随机配对；可进入模型明确支持的其他训练任务。
跨语言角色同名不证明同音色；多语言权重配置必须支持任意已接入语言，不能硬编码 en/zh。

基础 source_split=train 只是统一存储值。评估需按 speaker/recording/book/group 和重复关系隔离，
规则对应待评估能力；若所有数据都训练，recipe 明确 no_holdout，不能同时宣称有独立评估。

## 运行时

质量分数在 build 阶段转为非负有限 sampling_weight；全零候选集是错误。
权重不等于 sampler 已实现。sampler 必须执行指定的有/无放回算法，记录实际来源/语言分布。
按总 token 长度分桶或动态 batch，文本、参考、目标、特殊 token 都计入预算。

分布式读取器在 worker 中各自打开固定 snapshot，投影所需列并批量读取，不把音频表整库载入主进程。
保存 seed、epoch、rank 分配、消费位置/随机数状态；world size 改变时明确是否可精确续跑。
吞吐、RSS、存储读取量、GPU 等待率与重启一致性实测后才声明可用于全量训练。
Lance 存储本身不实现质量加权采样、packing 或模型 loss mask。

## Speaker 条件的两条路径

当前冻结 encoder 使用 `speaker_conditioning_mode=frozen_embedding`，未来解冻使用
`online_speaker_encoder`，详细字段和约束见 [11](11-speaker-embeddings.md)。
后者训练时需要读取参考音频并生成当前 frontend 输入；不强制把 mel 固化到统一层或 build 中。
因此“codes 已准备好”不代表解冻 speaker 的训练无需音频。

两种模式使用相同参考选择规则，recipe 显式固定，切换模式产生新 build_id。
speaker-only 不强制参考 codec；ICL 才按模型协议加入参考 codec/文本。
模型初始化方式与 codec 缓存分离：从头初始化 Talker 不使固定 tokenizer 的 codes 失效。
构建前验证 codec 码本顺序和值域、speaker embedding 的实际维度和注入位置，
不把相同维度误判为来自同一个 encoder。speaker encoder 更新后不得继续读取旧固定 embedding 替代 forward。
