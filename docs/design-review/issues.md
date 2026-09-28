# 实现状态与待解决问题

当前选择：Lance，contract/release v0.1。旧 Parquet 输出已删除，13 个来源全量转换已启动；Emilia2 暂不接入。
规范在 [data-contract](../data-contract/README.md)，这里单独记录实施边界。

## 已处理范围：MLS 上游英文坏包按用户批准排除

完整验收发现 `english/00128-of-00128/train-00000.tar.gz` 在结束前截断；独立 gzip/tar 读取复现。
转换已拒绝发布，原始文件未修改。已核对 HF 文件 API：云端大小与 LFS SHA256 均和本地完全一致。
因此当前上游对象本身截断，重下同一版本不会修复。用户已批准排除整个包。
配置：configs/source-exclusions/mls_sidon-v0.1.json；bulk 使用 --exclusions 显式传入，
验证路径/大小/SHA256 后排除，写入计划与发布 manifest。真实来源只读计划已确认选入 2,627 包，排除 1 包。
恢复要求相同排除策略，源内容变化会拒绝沿用规则。其他未知坏包仍失败并报告，不自动跳过。
此前逐包首条探查不覆盖包尾。路径、哈希与证据见 [MLS 复核](libriheavy-mls-check.md)。

## 当前已实现

13 个 adapter 已接入；新增九个完成真实前缀预览与配对测试，尚无整库音频验收。
共享 Lance fragment 写入、完整回读、sample_id 索引、单表发布与 checkpoint 恢复。
自包含来源采用 source-file-v1，Wenet 外部清单/评分采用 source-unit-v1；同一来源不因 worker/batch/输出分片变化而改变 ID。
Arrow 基础/视图/codec 类型与质量 struct 类型可生成；类型定义不等于完整任务执行器。

## 标注、视图、codec 与训练执行器

尚需实现生产标注 worker、结果唯一性/目标存在/输入指纹验证、协调发布与断点恢复。
列追加、稀疏乱序 merge、多版数组、快照和原音频不重写通过小规模存储测试，不表示生产标注流水线已完成。
视图父引用/边界/环校验、codec 模型推理、profile 发布、build 构建和分布式 sampler 仍待实现。
27 个基础字段包含派生关系，但当前 make_record 只供原始接入；派生音频还需要变换身份和完整父子验收。

## Lance 的性能与版本兼容

固定 pylance 12.0.0 / 文件格式 2.2。全量并发、索引构建、稀疏增列峰值内存、codec 训练吞吐尚无全规模基准。
本机 PyArrow 25 + Lance 12 的 pc.Expression 过滤遇到含 large_binary schema 的 Substrait unsupported type；
等价 SQL filter 走 sample_id BTREE 查询成功，当前示例使用经过验证的 SQL 路径，业务 ID 必须验证后构造。
当前音频使用内嵌 large_binary，不是 Blob 扩展；长音频 Blob/范围读取作为后续验证项。
快照引用登记与自动安全回收未实现，暂不自动执行 cleanup_old_versions。

## 延后：AAC 有效时间轴与现有解码假设冲突（Emilia2 接入前）

当前 schema.py 依赖 sf.info，实际 M4A 返回 Format not recognised。当前帧数检查也没有有效长度/padding profile。
需要选择并固定能应用 edit list 的解码路径，区分 decoded_num_frames 与 valid_num_frames，
把 profile 固定在发布/视图中；验证片段边界、重采样和 codec 输入的一致性。
源已接受漂移列表需要单独处理策略，不能自动“通过”。

验收至少包含真实 M4A 的 dialogue/long/short/ASMR、有尾 padding 和已知漂移样例，检查裁剪后波形与标注。
当前仅验证了抽样 JSON 坐标未越界，不是声学对齐通过。

## 延后：Emilia2 来源仍增加，当前快照不能声称全量

05:29 tar=885，05:40 tar=912。声明文件、历史验证清单和当前落地文件数量不一致。
固定要接入的版本与权威清单，确认完整文件的发布标记、大小/校验和、idx 与 tar 配套后再生成输入计划。
可以发布明确的子集版本，但不能把它命名为已完成全量。不能只依据 .tar 扩展名假定文件已下载完成。

## P1：来源关系、说话人、分数与文本需要逐 adapter 映射

- LibriHeavy configs：已全量比较 12,441,834 个 ID，配置内及 28 对配置间均无重复；此项 ID 级核查完成。
  Yue all/clean/eval 的完整成员交集仍待接入时核查；Wenet 本次单独核对三份完整 filelist。
- Emilia/YODAS 局部 speaker、游戏角色的不同语言配音、对话 S1/S2：固定范围；未知 speaker 不用于伪造同人参考配对。
- MLS：上游清单与本地 2,628 个 archive 完全匹配，每包第一组音频/metadata 均通过。
  旧 .opus 名、原录音秒数与 speaker 范围的现有映射已验证成立；详细覆盖边界见复核报告。
- Yue cut_point 为毫秒来源坐标，与当前音频长度可不同，必须验证时间轴映射。
- Wu 翻译、多个 ASR 版本、zenless 性别模板：固定文本选择/清洗版本；原始层保留原值。
- 原 quality 字符串、null、未知版本：明确数值解析、失败状态和来源版本，不能合成假 0 分。

## P2：来源完整性及资源类别

- Galgame：此前缺的六片已经出现，footer 读取成功，新增 21,079 行。此项结构性缺片问题已解决；
  未在本次重新做这六片全量音频校验和内容哈希。
- HiFiTTS2：URL 清单与本地文件仍差 2,454 项；转写和切片覆盖也需要明确，不能直接假设是完整监督 TTS。
- dns5：改用增强资产 schema；若所有来源都进入同一训练目标表，会把噪声/RIR 与语音目标混淆。
- Wuthering：四份 7z 成员配对检查通过，各语言 4 条实际音频解码通过；没有整包音频解码验收。

## P2：训练构建尚无通用实现

LM-TTS-Training 当前读取接口绑定 [T,16] 与 vocab 2048，多语言 balancing 分支只接受 en/zh。
逐样本 NPZ、主进程完整 manifest 行列表、每次读取校验的成本要用实际规模基准衡量。
新 pipeline 的 codec 应按特征分片输出；训练配方固定 token 空间、参考/目标协议、质量缺失策略、采样与评估隔离。
模型从头初始化不会消除数据泄漏、错误 speaker 配对和重叠视图重复加权的问题。


## 新接入的覆盖边界

详见 [适配与契约复核](adapter-contract-review.md)。
AISHELL-3 本轮读取基础 content.txt 的文字/拼音，不包含 prosody 标注的结构化导入；原包仍保留该信息，后续应作为标注任务接入。
VCTK 已知 p315 没有文本，两个 mic 不应被随机拆为互相泄漏的训练/验证样本。
游戏字幕可能包含非朗读模板/动作音；保留不代表可直接用于监督目标或同 speaker 克隆配对。
新适配器的音频解码验收仅覆盖实际预览样本；完整包尾、全库音频和跨来源内容重复尚待全量验证。
Wenet 临时磁盘峰值、重复读取共享清单及单包串行瓶颈须纳入全量性能测试。

## 全量运行发现并修复

- Lance 会跨工作线程继续消费同一个 Arrow 输入流。原 tar 配对 SQLite 默认绑定创建线程，
  小预览未触发，多批全量读取触发。已允许跨线程并用同一锁串行化数据库与 spool 访问。
- Galgame 的 Lump_of_Sugar_Kodomo_no_Asobi/train-00002-of-00003.parquet，零起始行 219，
  mit_b001073.ogg 为 0 帧；失败批次 9 个文件共 44,225 行头信息扫描只发现这一条。
  精确排除规则在 configs/source-exclusions/galgame-v0.1.json；不删除原始数据，不排除其他记录。
- 运行代码已隔离：未受影响任务使用 reports/runtime/before-cache-fix 快照继续，修复任务使用新代码。
  Galgame 已完成检查点保留实际旧代码哈希，显式兼容迁移后逐片复核再复用。

修复验收：73 项测试通过，含跨线程缓存、多批真实 Lance 写入、精确拒收和显式检查点迁移。
旧/新 Galgame 适配器的 64 条真实有效记录 ID、revision 与音频哈希一致；修复任务已恢复。


## Galgame 第二次拒收修复

batch-00001 的 10 片共 77,414 行音频头扫描发现两条 libsndfile malformed 错误：
ALcot_Clover_Day_s/train-00000-of-00003.parquet 行 3860（0050504.opus），
以及 train-00001-of-00003.parquet 行 7277（0041491.opus）。行号均从 0 起算。
独立 libopus 逐包解码分别得到 40,320 和 43,200 帧，全部包可解码；
OGG EOS granule 却分别为 40,321 和 43,201，libsndfile 1.2.2 在打开容器时报告错误码 3。
这是容器时间轴不一致；不应将其描述为音频包全部不可恢复。
当前原始接入精确拒收这两行，保留原文件；以后可在明确修复时间轴的派生流程中重新接入。
新策略复核源文件和音频 SHA256、原行号与 audio_ID，并再次核验指定错误码。
其他未知错误依然失败，不做宽泛跳过。全库其他未完成部分仍需完整转换验收。

为保持在跑任务的代码哈希固定，本次修复位于独立 Git worktree/分支
fix/galgame-malformed-audio；Galgame 恢复使用此版本，其他任务保持原运行代码。

## Galgame / LibriHeavy 精确拒收修复（2026-09-28）

已完整扫描四个失败批次的 37 个 Parquet 文件：Galgame 76,384 行、LibriHeavy 102,917 行。
Galgame 的 Windmill_Hatsukoi_Sankaime 两片混入 1,338 条 `.tag` 数据（24–96 bytes），
不具备可识别音频头；LibriHeavy large 三片有 27 条只有 OpusHead/OpusTags、没有音频包的记录。
这些记录按源文件 SHA256、原行号、上游 ID 和音频 SHA256 固定在各 dataset 的排除清单。
Galgame 累计排除 1,341 条（包含先前 3 条），LibriHeavy 排除 27 条。

五个受影响源文件逐条通过 adapter/schema 验证：29,523 条保留、1,365 条拒收；
所有保留行的原始位置均与源文件一致。79 项测试与 Ruff 检查通过。
未知错误仍失败；不修改原始 bytes，不自动扩大排除范围。contract/schema/release 保持 v0.1。
运行任务通过显式代码迁移复用原 checkpoint，实际旧代码哈希保留，恢复时逐片复核哈希。

本次恢复已启动，两个任务均为 64 workers。Galgame 保留 118 个完成检查点（7,026,443 行），
LibriHeavy 保留 283 个（9,331,886 行）；重启后进度先统计逐片重新核验通过的检查点，
因此计数会暂时低于保留数，不表示删除了已完成数据。
修复运行于 `reports/runtime/source-audio-fix` 的独立 Git 工作树。
旧 `galgame-malformed-fix`、`local-finalization` 工作树已删除；
`before-cache-fix` 和主工作树源码仍有其他任务使用，在这些任务结束前保持冻结。

## WenetSpeech4TTS Basic_6 缺失一条转写（2026-09-28）

完整扫描 `Basic/WenetSpeech4TTS_Basic_6.tar.gz`，132,677 个普通成员：66,339 个 WAV、
66,338 个 TXT，无重复、额外成员或特殊链接。与 Basic filelist 全量对账只缺
`X0000004983_6617014_S00105-S00111.txt`；其 WAV 存在，完整解码为 370,688 帧 / 16 kHz。
源 archive SHA256 为 `839320506976c980c8fdb115ceeec602232bfccb5365ae5c2ba3ca96b805f49e`。
用户批准缺项无法修复时丢弃该条，精确策略见 configs/source-exclusions/wenetspeech4tts-v0.1.json。
只排除这一个 source_key，预计本包输出 66,338 条；保留原始文件、源 ID 与所有正常记录。
恢复时验证缺失事实、存在音频哈希和来源依赖，未知错误仍失败，禁止自动跳过整包。

修复在 `reports/runtime/wenet-pairing` / `fix/wenet-pairing` 工作树，避免修改其他在跑任务源码。

修复验证：101 项测试通过；已恢复 24 workers，保留 50 个已完成检查点（2,632,768 条）。
恢复会重新核验已有分片，不重做已验证数据；其余未完成批次继续转换，最终状态以发布 manifest 为准。

同轮将 AISHELL3、原神、星铁的完成批次切换到本地 SQLite 收尾；三者均已正式发布，
行数分别为 88,035 / 654,252 / 403,437。生产收尾耗时（不含恢复分片复核）为
7.863 / 86.739 / 59.890 秒。沿用已有音频分片，固定 snapshot、索引查询和 manifest 一致性复核。
