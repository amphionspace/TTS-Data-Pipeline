# 已接入来源的精确排除协议

这些是当前 adapter 的来源规则，不是所有数据集的统一字段规范。通用发布约束见
[data contract 08](data-contract/specs/08-ingestion-validation.md)。

当前支持对 Galgame / LibriHeavy 已逐条核实的 Parquet 零帧、容器格式错误或无法识别的音频作精确排除：记录源相对路径、源文件 bytes/SHA256、原始零起始行号、
上游 ID（Galgame 为 audio_ID，LibriHeavy 为 id）、音频 bytes/SHA256、condition 和 reason。
必须核验源文件与记录身份。zero_decoded_frames 要求读取器报告 0 帧、实际读取也返回 0 帧；
sndfile_malformed 要求当前固定版本读取器在打开音频时报告 SF_ERR_MALFORMED_FILE (3)；
sndfile_unrecognised 要求报告 SF_ERR_UNRECOGNISED_FORMAT (1)，且该条记录已经人工核实并列入内容固定的清单。
其他异常、目标恢复可读或身份不匹配均失败。这两种错误条件只声明当前读取器拒收，不声称其他解码器也无法恢复音频。
不得重编号原始行号；未排除样本保持原 ID。manifest 保存 excluded_source_records 与 rejected_rows，
输出行数 + 明确排除数 = 源 Parquet footer 行数；查不到排除目标或出现未知错误都不能发布。
排除清单纳入固定计划，恢复要求完全相同；不得把空音频修复为伪造波形或自动排除整文件。

Wenet tar 的已核实缺失转写允许按单条 source_key 精确排除，condition=missing_transcript。
清单固定 archive 相对路径/大小/SHA256、source_key、存在的 audio_member 与 bytes/SHA256、
缺失的 missing_member 和原因；不伪造 Parquet row。外部 filelist/DNSMOS 仍作为语义依赖固定。
读取必须确认目标在 filelist 中、音频内容匹配且只出现一次、整个 archive 中没有该转写 member。
目标转写重新出现、音频不存在/变化、额外未知缺项或策略目标未找到均失败。
输出 source_key 集合与明确拒收集合的并集必须等于声明 filelist，二者不得重叠；
其精确拒收同样进入 excluded_source_records/rejected_rows，不排除整个包，也不生成虚构文本。
