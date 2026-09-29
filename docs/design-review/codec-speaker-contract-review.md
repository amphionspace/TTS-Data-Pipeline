# Codec / speaker 特征约定与训练仓库对照

日期：2026-09-29。用户要求先定 codec 与 speaker 特征，不展开 annotation。
用户已确认：当前 speaker encoder 冻结，未来可能解冻；不将某一种 mel 固定为统一格式。
本次更新规范、Arrow 类型描述、合成示例和存储可行性测试，不运行 GPU 提取、不修改训练代码。
现有音频转换仍运行固定 recovery-20260929 工作树，不受主工作树 contract 描述修改影响。

## 参考版本与观察

参考 LM-TTS-Training commit 87e026770e6c94c0e912b2e678600611aa558af5，读取时工作树干净。
未下载或加载真实模型权重，不宣称已完成 Qwen encoder 生产验收。

| 本地证据 | 观察 | 新约定的处理 |
| --- | --- | --- |
| qwen3_train/data.py:CodeDataset | JSONL 索引 + 每条 NPZ，期望 [T,16]、0..2047；转 torch.long | 特征统一为 Lance，Qwen profile 采用该候选 shape/range，通用 K 不写死 |
| scripts/prepare_emilia_streaming.py:encode/save | float32 波形传 sr=24000；返回 codes 转 uint16 后保存 NPZ；缓存含 batch_size=16 | 先验整数/范围再存 int16；有效长度与 padding 验证；Lance 内容摘要不等于 NPZ 文件 hash |
| scripts/download_models.py | 固定 tokenizer repo revision 7dd38ad4e9bad454aae9cd937d0cd577604fe229 | 作为候选来源，不当成本机已核验权重 |
| qwen3_train/assembly.py:audit_sources | 校验 num_code_groups=16、vocab_size=2048、speaker enc_dim 与 Talker hidden size | 特征表独立 K/D，build 验证模型注入兼容；不能因 K 相同就混 codec |
| qwen3_train/assembly.py / model.py | 支持 lm_tts_freeze_speaker_encoder；可加载预训练 speaker 权重 | 冻结时离线 embedding，解冻时原音频/view → frontend → 可训练 encoder |
| qwen3_train/speaker.py:audio_mel | 24k、128 mel、FFT/win=1024、hop=256、fmax=12000 | 记录为当前候选 frontend 参数，不作为统一存储必需列 |
| qwen3_train/model.py:input_embeddings | 按有效 mel 长度分组，避免 padding 进入 ECAPA pooling；encoder 输出再进入 Talker | 保存 encoder 原输出，离线批处理须验证 padding 与 pooling；不能缓存加了 text_pad 的条件向量 |
| qwen3_train/train.py | 训练 target_speaker=True，full_target_audio；生成选另一个训练 utterance | reference_policy 显式选择；统一特征按片段提取，build 再绑定参考 |
| qwen3_train/sources.py / speaker.py | 不同输入分支用 scipy.resample_poly / torchaudio.resample；旧 Emilia 路径涉及 AAC 帧尾策略 | 不将这些路径视为数值等价；新 profile 固定实际算法；不把 AAC 补尾策略泛用到所有 MP3/FLAC |

参考仓库的 backbone 权重装配策略不是本任务的决定；用户仍计划 Talker/base model 重新初始化。
特征仅绑定 tokenizer / speaker encoder 本身及前处理，不因无关 Talker 初始化改变而重新生成。

## 已明确的结构

- dataset/release 下各自 codec 与 speaker_embedding，按 profile/run 发布独立 features.lance。
- base 不变、原始音频保留；两类特征共同使用目标 ID、父 sample、时间区间、输入指纹。
- codec [T,K] int16/int32；speaker [D] float32。索引按 target_id/feature_key，view 加 parent_sample_id。
- 当前冻结 encoder：训练 build 内嵌已选择参考 embedding；以后解冻：保留固定 base/view 引用，在线提取 frontend。
- mel 非必需、无默认 mel 表。将来有实测性能需要再设独立 frontend profile 缓存，不影响 canonical raw audio。
- 纯音频缓存不绑定文本修订；codec 与 speaker 独立失效。
- 公布 run 必须目标全量终态记账，失败仍明确保留；不得把缺失结果用零向量/空 codes 伪装成功。

## 仍待实际实现与验证

1. 真正选择/加载权重，核对 K、vocab、D、有效输出长度、frontend 隐含参数、权重摘要和环境。
   profile 示例中的 null 必须解决；示例明确 non-runnable，不能用于生产。
2. GPU 特征执行器、bounded CPU decode/queue、并行写 fragment、结果验证与覆盖/恢复协调器尚未实现。
3. LM-TTS-Training 目前不能直接读新的 Lance features/build，且不提供缓存 speaker embedding 的输入分支。
   需要独立适配 CodeDataset/collate/model forward，保持音频在线路径和训练梯度正确。
4. 旧 NPZ 不能只按旧 id 直接认领为新特征。迁移需核对实际音频、区间、波形处理、权重、长度与输出数组摘要。
5. speaker 自身作为参考/另一条参考影响实验语义；新 recipe 明确选项，规范不偷偷把旧实验当独立参考训练。
6. 批处理等价性、重建听检、多语言/采样率样本与长音频/失败恢复、真实 GPU 吞吐和训练等待率待测。
7. 上述是数据/缓存结构验收，不是 annotation 设计；未定义新的质量打分或标注执行计划。

验证：5 项定向测试通过，覆盖既有 base contract、codec 嵌套整数数组、speaker float32 固定向量、
失败 null、真实 Lance 标量索引读取与追加后的旧 snapshot 保留。Ruff 检查通过。
这是存储结构测试，未执行真实 codec/speaker 推理；规范字段仍是未来执行器必须满足的约束。

## 独立完整 review（2026-09-29，原始发现，现已修订）

范围：codec/speaker 章节、引用的 base/view/identity/build/manifest/生命周期规则、Arrow 描述、
profile/行/manifest/recipe 示例、检查器和存储测试，并对照 LM-TTS-Training 当前接口。
annotation 仅检查公共 manifest 规则是否误用，不在本轮重新设计。
以下保留修订前的发现及原始行号，四项修复状态见文末；不代表当前规范仍有这四处矛盾。

### R1 · P1 · 浮点容差与相同 feature_key 的逐字节一致性未协调

06-codecs.md 第 46–49 行将实际 GPU/batch 作为 execution 信息，要求验收 batch 独立性；
第 100–101 行又要求同一 run 相同 feature_key 的输出摘要完全相同。
11-speaker-embeddings.md 第 93–94 行承认冻结/在线输出按浮点容差验收，跨 GPU 不天然逐字节相同。
若同内容的两个 target 分别在不同 batch/GPU 上计算，结果可以通过浮点容差却因 SHA256 不同导致发布失败。
当前没有规定相同 feature_key 必须只算一次并分发同一权威结果，重试冲突也未定义如何裁决。

最小演示：float32 向量 [0.125,-0.25,0.5] 的第一个值向正方向移动一 ULP，
最大差 1.4901161193847656e-08，rtol=atol=1e-6 时 allclose=True，规范数组 SHA256 不同。
这是规则不等价的演示，不是声称已运行真实 Qwen/GPU 实验。
PyTorch 2.8 官方文档明确不保证数学等价的批处理/单条计算及跨平台计算逐字节一致：
https://docs.pytorch.org/docs/2.8/notes/numerical_accuracy.html

建议：区分计算身份、具体输出 artifact 摘要和数值验收。run 内相同 feature_key 复用同一个已验收结果；
如发生独立重算，规定执行域、候选结果选择与冲突处理。磁盘完整性仍用精确 hash；
speaker 浮点验收使用明确的 atol/rtol（必要时附余弦/范数），codec 离散 token 不直接套浮点容差。
不能以本次 allclose 演示证明任意两个 encoder 结果可混用。

### R2 · P1 · coverage 的母集对 view 任务不成立

10-manifests.md 第 49–51 行将 table_rows 定义为 samples snapshot 行数，并无条件要求
coverage.total_targets <= table_rows。06 第 167 行同时支持 all_views。
一条 base 长录音可以有 100 个合法 views；这时 feature total_targets=100，base table_rows=1，
按通用规则会拒绝一个完全有效的 view feature run。feature 示例又没有 table_rows，
当前检查器只对 annotation 示例检查这个约束，不能发现继承规则的矛盾。

建议：samples 列内结果保留原口径；独立 feature run 以 selection 中的 target 集合作为母集。
如要记录 available_target_rows，sample 指 samples，view 指 views；parent/base 行数另记，不能用来限制 view 数。
明确不同 artifact/storage_kind 的 coverage 条件，不让 annotation 的母表规则扩散到 feature。

### R3 · P2 · 必需的子集选择表尚无发布协议

06 第 168 行要求任何子集先发布不可变选择表，并绑定 table snapshot 和 manifest/hash。
但目录、Arrow schema、manifest 的 artifact_kind 集合和示例只定义 base/annotation/view/feature/build/asset，
没有指定 selection 表的归属、行类型或引用方式。实现筛选后只提取少量 codec 时无法照现有文档完成闭环。

建议：采用简单明确的一种形式，例如作为 feature run 的一个固定输入表，规定 target_kind、target_id、
输入 snapshot 引用、唯一性、孤立 ID 校验、行数/目标集合摘要及保留规则。
如果独立发布则同步定义 artifact_kind/目录/schema；不要让不同实现自行命名、拼接选择文件。

### R4 · P2 · 在线 speaker 路径仍继承了不应必填的特征引用

07 第 29–30 行要求每个训练记录都有目标/参考 feature_key；11 第 72–73 行要求参考特征
profile/feature_key/摘要；但 11 第 77、86–88 行允许在线 encoder 只读参考音频，不生成离线 embedding。
speaker-only 模式又明确不要求参考 codec。此组合没有任何参考 feature 可引用，不能同时满足前述必填规则。
现有 recipe 增加了 mode，但没有按 mode 列出字段存在/互斥/可空规则。

建议：固定条件矩阵。frozen_embedding 必须有 embedding 引用/值；online_speaker_encoder 必须有
固定音频引用、原生区间和 frontend profile，不要求 embedding 引用；reference codec/text 仅在模型协议需要时必填。
显式拒绝 trainable=true + frozen_embedding、缺 online 音频引用、未声明 ICL 却隐式要求参考 codes 等不一致组合。
self 与 other_same_speaker 的参考约束也要条件化，避免只修改 reference_policy 而留下互相矛盾的 allow_same_segment。

### 验证边界与保留决定

本轮重新运行 contract 检查：25 个文件的 JSON/YAML、文档链接、生成 Arrow 描述和 deployed 副本一致性通过。
它不是生产 profile/feature 的语义验证器。已有 5 项测试验证基础 contract 和 Lance 存储操作，
不覆盖实际编码器、全部状态/指纹约束或训练 forward；候选 profile 的权重、D、有效长度、padding 仍未实际验收。
这些未实现项已在上一节明确记录，不能误报为已完成，也不将示例中有意保留的 null 本身当作新 bug。

保留：Lance 按 dataset/release/kind/profile/run 组织；原始音频长期保留；codec 与 speaker 独立失效；
embedding 属于片段而非每个 speaker 的单一向量；mel 不作为统一必备产物；冻结/在线两条路径；
原生采样帧和 codec 帧区分、训练固定快照和实例引用。首版不必为上述问题推翻整个目录结构。

## 修订结果（2026-09-29）

R1–R4 均已修订到规范、示例与检查器：

| 项目 | 修订 | 验证 |
| --- | --- | --- |
| R1 | 同 run/key 首个已验证持久化提交为权威结果；候选重算只能验收，不覆盖；codec 精确整数、speaker 显式容差 | 一 ULP 浮点变化通过容差但改变摘要、超界失败、相对容差操作数方向、整数 token 禁止浮点容差 |
| R2 | sample_column 的母表限制不用于 feature；feature 以固定 samples/views 目标表计数 | 一个 parent、100 个 view targets 合法；超范围、缺终态、错误计数拒绝 |
| R3 | subset 在 run 内冻结 targets.lance；三列非 null schema，selection.table 固定 snapshot/schema/行数，集合摘要绑定内容 | 合成双 view 共用一个 parent，真实 Lance 读写/索引/旧 snapshot；重复或乱序目标拒绝 |
| R4 | 明确 frozen/online 与 speaker-only/ICL 条件矩阵及 self/other_same_speaker 开关 | 六种合法组合；缓存与解冻冲突、缺音频、缺 codes、残留 embedding、矛盾 self/codec 开关及缺参考身份拒绝 |

27 项定向测试通过（基础 contract、feature 存储与新增回归测试）。规范检查覆盖 28 个文件：
JSON/YAML、链接、Arrow 描述、示例覆盖数、目标集合摘要与模式条件；新增 profile 模板仍 runnable=false。
feature_contract.py 是有限范围的规则检查函数，不是完整 feature 发布器：真实输入外键、所有行指纹/摘要、
生产 profile、模型推理与训练 forward、协调提交/崩溃恢复仍需执行器实施和实测。
基础 27 列与 v0.1 保持不变；不扩展 annotation，不启动特征生成任务。
Ruff 与 git diff --check 通过；同步后 28 个规范文件与 DATA-TTS-UNIFIED 副本逐字节一致。
