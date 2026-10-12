# Selection

规则版本的定义见 [12 · 用途规则版本](data-contract/specs/12-selections.md#用途规则版本)；本页记录规则文件、发布结果和执行方式。

| 版本 | 规则文件 | 已发布 selection | 状态 |
| --- | --- | --- | --- |
| selection-v2 | configs/selections/selection-v2.json | `tts-selection-supervised-tts-20261012T045922bjt-01` | 当前，一阶段预训练 |
| selection-v1 | configs/selections/selection-v1.json | `tts-selection-supervised-tts-20260929T151539bjt-01` | 现有训练 build 使用，保留 |

规则文件与发布目录中的 rules.json 逐字节相同（SHA256 一致）；改文件名不改内容，以免与已发布摘要不符。
selection-v2 的 rules.json 内部 rules_version 为历史名 `selection-filter-v1`。

## selection-v2 发布记录

- 输入 18 个已发布 base v0.1：selection-v1 的 16 个，加 zenless_voice、emilia2（ASMR 已在接入时排除），共 235,079,775 条 / 520,518 小时。
- 父 selection 为 selection-v1 的发布，完整继承其 8 条有效排除。audio_stats 已停用并删除，不使用；不使用 DNSMOS；不做留出划分。
- 文本来源：genshin/starrail/wutheringwaves/zenless 用 `tts-ann-transcription-20261010T111659bjt-01` 主转写（来源码 1）；
  emilia 用 `tts-ann-transcription-20261010T171514bjt-import` 上游 Qwen ASR 导入（来源码 2）；其余 13 个（含 emilia2 的上游 Qwen ASR 文本）用 base text。
- 来源时长证据为 `reports/interval-audit/findings.tsv`，除 `invalid_word_timestamps` 外 14,250 条记 105：
  来源 duration 比实际音频长 0.05–1 秒（emilia 1,416、emilia_yodas 12,814、libriheavy 19、mls_sidon 1）。
  wenetspeech4tts 的 34 条只涉及词级时间戳，完整 WAV 保留。
- 结果：入选 204,053,470 条 / 477,713 小时。主要排除：2002 不支持语言 1,606 万（emilia2）、4001 重复非代表 634 万、
  3001 不足 1 秒 520 万、2004 文字系统不符 251 万、1007 标记 46 万、2003 语言证据 10 万、4002 冲突重复 10 万。
- 18 个分支均经独立回读：base main 版本未变、行数一致、原因计数与 manifest 一致、入选行选用文本非空。
  检查脚本、各轮 dry-run 报告与日志在 `artifacts/selection-v2-20261012/`。

规则只为多行、可复现的系统性模式添加，不为单条样本加规则。各轮发现：

- emilia 与 emilia_yodas 共享约 480 万条相同音频，emilia 使用新转写，因此 emilia 成员为代表。
- emilia 上游部分 ZH 分片（如 ZH_B00021、ZH_B00066）实为英语音频，约 216 万条、5,600 小时；
  base 与新转写都是英文，只是语言标签错误，由 2004 排除。
- emilia2 约 1,606 万条（16,244 小时）为允许集合外语言（ar 1,047 万、ru、hi、fa、yue、bn、th 等），按 2002 排除；扩充语种需新版本。
- libriheavy 约 3.8% 文本用 `_斜体_` 下划线标强调，词本身朗读正确，不排除；最终训练数据构建时可去掉下划线。
- mls_sidon 文本全小写无标点，属来源风格，不排除。
- 游戏 ASR 文本有专名同音误写（如“眼狩令”→“演兽令”），预训练可接受；不做逐条修正。

## selection-v1 发布记录

输入为 16 个已发布基础快照，共 134,832,658 条。按规则保留 1–120 秒、文本有效、语言可识别；11 种语言全部允许。
固定鸣潮 32 条一帧音频及 Galgame 7 条已审阅长音文错配；相同坏音频 hash 合并为一个排除作用键。
音频存在性继承已发布基础接入的非空 bytes/哈希/全量回读验收，不重新解码全部 18TB；coverage 明确这一范围。
来源优先级不表示质量评分，Emilia 先于 YODAS；未入选记录仍完整保留。

## 读取

选用文本用 `selected_text if selected_text is not None else text`，不能用 `selected_text or text`；
选用语言同理以 `selected_language` 优先。只读 `selection_reason = 0` 的行。

## 执行

```bash
python scripts/build_selection.py plan --work /tmp/tts-selection-<run> \
  --rules configs/selections/selection-v2.json
python scripts/build_selection.py run --work /tmp/tts-selection-<run> --workers 80
```

plan 只读基础数据，固定输入 manifest 哈希、快照、fragment 分组、父 selection 排除、annotation 输入、证据文件和规则，
并记录执行器代码摘要；代码改变后旧计划拒绝继续，须重新 plan。run 依次 scan → deduplicate → publish，
不包含 plan；也可以显式运行各个 phase 恢复。scan 前先把 annotation 按 base 行序对齐到本地 parquet。

扫描按约 25 万行的 fragment 组拆分，每进程限制 BLAS/Lance CPU 线程为 1、IO 线程 2。
大型来源内部并行；全局分组统一协调。最后不同 dataset 分支并行写入，最多 16 个协调者，同一表单写者。

局部有界扫描产生本地 mmap 检查点；随后构建完整 hash 全局排序索引和决策数组。
这些数组只在 /tmp 用于计算/恢复，不作为发布格式；selection-v2 的 18 个数据集工作目录约 108GB。
中断扫描复核片段文件 SHA；重复阶段未完成则由原始检查点重新构建，不沿用部分被修改的原因码。
已提交分支可全量核验后复用；全体输出完成才原子发布 selection manifest。
发布失败留下的未完成分支须删除后再用新 plan 重跑，不得被消费。

每条分支回读核对 sample_id、reason、flags、最终文本覆盖及其与审计文本哈希一致；保护 base/输出 tag，main 和基础文件引用不变。
分支的输入定位使用固定快照原生 row address 并校验 sample_id，拒绝含删除行的 base；不靠回调调用顺序。
最终输出为 selections/<selection_id>/ 与各 samples.lance 的同名分支，用户读取只依赖这些正式产物。
发布并完成回读后，本地数组和扫描检查点可清理；运行中不可删除。
