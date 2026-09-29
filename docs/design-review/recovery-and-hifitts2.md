# 2026-09-29 · 最后失败批次与 HiFiTTS2

## LibriHeavy

完整扫描 batch-00316 的 9 个 Parquet、33,927 行。发现 49 条 245-byte OGG，
均只有 OpusHead/OpusTags 两个包，没有音频包，libsndfile error 3。
位置为 default/large/shard-00017-of-0020/large-00004-of-00148.parquet；
精确行号、ID、文件与音频 SHA256 在 configs/source-exclusions/libriheavy-v0.1.json。
累计拒收从 27 增至 76 条；预计最终 12,441,758 条。原始文件不修改。
保留已完成 372 批的 12,407,880 条，不改写这些 checkpoint 的实际代码版本。

## Emilia-YODAS

DE/DE-B000049.tar 为 1,043,456,000 bytes，SHA256
61f48385613fb795161ccc04d7af9b6f32b20e5a6b60d49e58b96f0454ae81e9。
Python tarfile 读出 1,434 个成员后，在 offset 59,815,424 遇到无效 header 并停止。
该位置不是正常零填充，而是非零 MP3 数据；下一段有效 PAX header 从 59,931,136 开始。
严格 EOF 校验成功阻止了静默截断接入；报错文字中的 terminator 并不证明遇到了有效结束标记。
不能据此推断整个包的所有音频都坏了，也不应猜测缺失边界或放宽校验。

本次整包隔离，通过 configs/source-exclusions/emilia_yodas-v0.1.json 固定文件哈希。
原文件保留。排除的是 1 个源 tar，绝不是只排除 1 条样本；包内记录数不以部分可读数冒充。
保留 607 个完成批次的 43,928,398 条；batch-00012 剩余 3 个包继续转换。
恢复计划仅删除失败批次内该输入，其他批次名称与成员保持不变，避免重新分组失去检查点。
发布 manifest 记录 excluded_source_files；未来修复源包需按新快照重新接入。

## HiFiTTS2

输入是用户指定的 /workspace/workspace/yanglin/hifitts2_work/parquet_22khz，
不是 DATA-TTS 中旧章节下载目录。全部 5,284 个 Parquet footer 核对：
12,809,875 行，2,935,155,378,852 bytes，Arrow 字段结构一致，与本地 summary.json 一致。
本地上游已排除 283,093 个 utterances / 2,909 个失败章节；这些未进入 Parquet 的记录
不属于此次转换输入，不能将新发布描述为官方全库无损接入。

- dataset_id=hifitts2；contract/schema/release=v0.1。
- source_key=audio_filepath；source-file-v1 固定各 Parquet 路径与 SHA256。
- locator root=hifitts2_parquet，对应上述本地目录；path=data/shard-*.parquet，row 从零开始。
  这个 root 是部署时的路径别名，不把机器绝对路径写入逐行身份。
- 输入已经是上游处理后的 22,050 Hz 单声道 FLAC；本转换保留这些输入 bytes，不再次切分或重采样。
- text 保留源 text，normalized_text 放 source_normalized variant；language=en。
- speaker_id=hifitts2:<speaker>，scope=dataset；group_id=hifitts2:book:<book_id>。
- 所有输出 source_split=train；metadata.original_split 保留 train/dev_seen/test_seen/dev_unseen/test_unseen。
  将来训练构建需要显式选择划分；不能将这些官方评估样本误当作未见数据。
- metadata.upstream 完整保存源 metadata_json；metadata.parquet_fields 保存所有独立标量列，
  包括 wer/cer/bandwidth/speaker_count/text_source/duration。重复字段必须一致，冲突失败。
- duration_seconds 来自实际 FLAC 帧数；上游 duration 单独保留。annotation/codec 本次不生成。

验证：106 项测试通过；32 片共 128 条真实抽样全解码；完整最小分片 987 条转换、
Lance 读回和全解码通过，源哈希与 footer 行数对账通过，用时 8.43 秒。
抽样不代表全库音频已验证；全量任务还将遍历每条记录并验证全部输出。
按用户最新要求，全量使用 128 workers，1 GiB 目标 shard，4 GiB 输入批次，输出 datasets/hifitts2/v0.1。

## 启动记录

已从固定 runtime recovery-20260929（代码提交 2bc07eb）启动，三个任务均为 128 workers。
LibriHeavy 保留 372 个 checkpoint，Emilia-YODAS 保留 607 个；恢复时重新核对分片 SHA256。
HiFiTTS2 首次全量启动，输出 /workspace/data/DATA-TTS-UNIFIED/datasets/hifitts2/v0.1。
运行中的进度以 reports/current-conversion/launch.json 和状态脚本为准，启动不代表全量已完成。
