# 07 · 训练数据构建、计划与读取

## 复用样本分支与特征，不先复制训练全集

首选验证布局为 indexed_references，发布前必须通过端到端训练读取基准。
固定 [12 selection](12-selections.md) 的全体分支、规则和 manifest 哈希；从各固定分支派生 build 分支，
批量关联目标特征，在原行上增加 codec_row、speaker_row 和 build_ready。
绑定使用固定 feature snapshot 内的逻辑行偏移；其来源 target_id、audio/profile/区间/摘要逐条验收。
它不是稳定业务 ID，也不是 Lance _rowid。不能把 _rowid 当作 take 的行偏移，不能使用隐含相同行序。
一行失败/未生成特征时 locator=null、build_ready=false，保留原因统计；selection 的数据原因不被覆盖。
首版 self 协议下 speaker_row 指向 target 自身 embedding。
首版每dataset每kind绑定一张表，表引用在manifest记录一次。后续同profile的subset补算可复用旧成功表，
确有多个run时才另加nullable codec_run_slot/speaker_run_slot（uint32），按manifest固定字典定位具体表，
与row共同构成定位；不能用同一个裸row隐式跨表。新build一次解析重叠成功结果的选择顺序，训练不查fallback。

build 位于 builds/<build_id>/{manifest.json,data_recipe.json}，不默认有 records.lance，
manifest bindings 引用各 dataset 的 build branch/version 及选定 codec/speaker run/table/version。
data_recipe 固定 selection、profile/run、文本/语言规范化、文本 tokenizer、参考协议、特征定位布局。
build_id 使用 `tts-build-<name>-YYYYMMDDTHHMMSSbjt-NN`，完整规范 data_recipe 的 SHA256 另存；
可读名字不代替内容摘要。更换特征快照或模型输入协议需新 build，禁止沿用旧 row locator。
采用05/12的稀疏selected_text覆盖值及固定规范化，不在每个step跨annotation读文本；
语言优先读取selected_language覆盖，null才回退base；alias规则固定在selection。
音文指标须匹配实际text_revision。文本 token/长度可另增小列，原 text 不复制；是否缓存由真实 CPU/I/O 吞吐决定并固定版本。

训练 plan 位于 training_plans/<plan_id>/{manifest.json,recipe.json}，固定 build 哈希、
采样权重、seed、预算、分布式恢复与评估策略；ID 同样采用可读名称和北京时间，另存 recipe_sha256。
只改采样权重不更新样本分支、不新算特征、不再复制 codes。plan 不能越过 build_ready 和 selection 的入选范围。
扩充入选范围需新 selection；若新增目标已有兼容特征，可重新绑定复用，不因文本/权重变化重算纯音频特征。

训练按批量 locator 读取固定 features snapshot，做局部性调度/有界缓存，保持 draw 顺序及目标身份对应。
索引查询用于稀疏调查和构建绑定，训练每步不能全库 join 或逐条随机查询共享盘。
绑定表和 sampling metadata 可以投影读取；不加载音频或全库 Python 行对象。
端到端验收失败时先优化批读取和缓存；确需 materialized_codes 必须列出存储预算与吞吐收益，
作为单独 build 显式选择，不能悄悄成为默认。物化 build 仍可被多个 plan 复用。

当前训练仓库读取器尚不支持上述绑定，不要求保留旧逐样本 NPZ 格式。
公开输入固定 manifest/table/branch/version；只读取 complete，不用 latest。
本规范不表示读取器、绑定构建器或通用 weighted sampler 已经实现。

## 参考协议：必须显式选择

首版训练协议固定为 reference_policy=self，speaker_only 条件；这与现有训练代码取 target 音频的行为一致。
self 的 reference_scope=target、reference_count=1，不要求来源 speaker 标签；同片段/重叠明确允许。
Galgame/Wenet 不因没有 speaker 就被这个训练协议排除，但仍要通过音频和音文质量筛选。
自参考不能作为独立参考音色克隆能力的评估结果。

other_same_speaker 必须设置 reference_scope=same_recording 或 cross_recording，并有同 speaker 证据、
非重复且不重叠的参考、明确 recording 身份、max_reference_pool_size 和确定的池选择规则。
unknown recording 不能冒充跨录音；上游局部 speaker 标签不能自动升级为全局身份。
Emilia 标签含 batch 范围，不能从 batch 相同推出录音相同；YODAS 标签为视频内范围，均不能直接承诺跨录音配对。
缺 speaker 的样本不能用于 other_same_speaker，除非另有已验证身份标注。
参考池大小按协议明确为正整数，不默认“每人几条”；self 不设每人上限，每个目标需要对应音频区间的条件。
训练和评估分别声明参考协议，不因训练 self 就让克隆评估使用 self。

冻结/在线 encoder、speaker-only/ICL 的条件字段沿用 [11](11-speaker-embeddings.md)。
已有 other_same_speaker 模板必须补充 reference_scope 和池上限才能成为生产 recipe。

## 全局去重与评估隔离

精确重复在 selection 发布前跨所有输入统一裁决，关系证据归 selection，分支原因记录采用结果。
plan 不能重新放回 duplicate_not_selected 或 confirmed_mismatch；调整裁决应生成新 selection。
首版 selection 的 evaluation=not_assigned 不代表已经隔离；启动训练必须显式声明 no_holdout，
或先发布并验证隔离后的 selection。纯 codec 可以覆盖多个 split，但训练 draw 不能包含评估目标。

seen_speaker 评估与 unseen_speaker 评估分别声明。前者可按规则允许训练中出现的 speaker/参考来源，
但目标及其重复/重叠片段不得泄漏；后者隔离确认的 speaker，参考池必须来自允许的评估侧数据。
目标和参考都需检查精确重复、已知重叠、录音及所选 speaker 隔离条件。
近重复覆盖不足、未知身份和未知录音需报告，不能声称全量泄漏已排除。
基础 source_split=train 仅是存储值，不代表完成了评估划分。

## 加权采样与分布式验收

plan 固定 sampling unit（utterance/duration）、来源/任意语言权重、质量映射、缺值策略、归一化范围、
有/无放回算法、seed、epoch 样本或 token 预算、参考选择算法、world size/rank 分配和续跑规则。
权重为非负有限数，候选权重全零拒绝。质量与语言混合时明确归一化顺序，不把行数概率误称为时长比例。

真正的 weighted sampler 先产生抽样序列，再按文本、参考、目标和特殊 token 总长度 packing。
必须记录并验证实际来源/语言/质量分布、丢尾偏差和重复抽样；评估样本不允许通过补齐进入训练。
恢复需固定 seed、epoch、消费进度和随机状态；world size 改变时说明精确恢复边界。
当前参考仓库的 sampler 有长度打包和仅 en/zh 的时长平衡，尚不是通用多语言质量加权 sampler。

生产前必须通过：固定权重的统计分布验证、任意语言、零权重、多 rank/worker、断点恢复、参考隔离，
以及真实训练步的 codes/embedding 或在线音频读取吞吐。仅 encoder 推理快不能代表训练读取已验收。
