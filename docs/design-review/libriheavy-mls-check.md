# LibriHeavy / MLS 来源复核与身份修复

核查日期：2026-09-28。本文保存完整配置 ID/路径检查的结论与覆盖边界。
当前身份算法见 [v0.1 约定](../data-contract/specs/03-identity.md)：LibriHeavy / MLS 为自包含来源，
采用 source-file-v1；改变 worker、batch、分片和根目录不改变身份。
后续新增的外部语义依赖使用 source-unit-v1，不能套用单文件规则。

## LibriHeavy：不存在单独的 HF all 配置，拼接全部八份是正确的

直接核对了 [HF 原始仓库数据卡](https://huggingface.co/datasets/mythicinfinity/libriheavy/blob/main/README.md)
及 [LibriHeavy 官方仓库](https://github.com/k2-fsa/libriheavy)。HF 数据卡列出八个配置，
每个都通过名为 train 的 split 读取；其 all 用法是遍历这八个配置后 concatenate，并非名为 all 的配置。
官方仓库进一步区分 large 训练集合与 large/medium/small 三份物理清单。

本地读取全部 3,292 个 Parquet 的完整 id 列，使用 SQLite 按原始字符串做精确比较：

| 配置 | 文件数 | 记录数 | 配置内重复 ID |
| --- | ---: | ---: | ---: |
| small | 32 | 122,526 | 0 |
| medium | 287 | 1,101,040 | 0 |
| large | 2,955 | 11,156,740 | 0 |
| dev | 2 | 5,348 | 0 |
| test_clean | 1 | 2,557 | 0 |
| test_clean_large | 7 | 26,127 | 0 |
| test_other | 1 | 2,815 | 0 |
| test_other_large | 7 | 24,681 | 0 |
| 总计 | 3,292 | 12,441,834 | 0 |

全部 28 对配置的 ID 交集均为 0；总唯一 ID 数等于物理记录数。
因此当前 bulk 选择八个配置全部接入的行为正确，不应改成只选 large，也无需按这些 ID 去掉某一配置。
统一输出 train，同时保留原 config 和上游 split；未来训练仍可根据原 config 排除评估来源。

这是完整 ID 级核查，不是对所有音频做感知去重；不能推导“相同内容绝不使用不同 ID”。
此前“配置交集尚未核对”的问题现已在 ID 层面关闭。

复核脚本：`scripts/check_libriheavy_overlap.py`；结果：`reports/source-check/libriheavy-overlap.json`。
它只读取 id 列，不加载音频 bytes；临时 SQLite 在检查完成后自动清理。

## MLS SIDON：来源清单和字段映射成立

核对 [HF 原始仓库](https://huggingface.co/datasets/sarulab-speech/mls_sidon)、
[上游 paths.yaml](https://huggingface.co/datasets/sarulab-speech/mls_sidon/blob/main/paths.yaml)
和 [原始 MLS 发布页](https://www.openslr.org/94/)。下载的 HF paths.yaml 与本地文件 SHA256 完全相同：
`3244c39b4142163af34dc34b40c149fd1ea600490d831af56ccbdb7f8589f73d`。

清单共 2,628 个 archive，本地路径集合完全相等：缺失 0，多余 0。
随后每个 archive 读取第一组完整的 FLAC + metadata.json，覆盖八种语言及原始 train/dev/test：

| 语言 | 检查的 archive / 样本数 |
| --- | ---: |
| English | 2,307 |
| German | 103 |
| Dutch | 80 |
| French | 58 |
| Spanish | 47 |
| Italian | 15 |
| Portuguese | 10 |
| Polish | 8 |

2,628 个样例均通过成员 ID 配对和音频头读取，均为 48 kHz 单声道，均有 transcript、speaker_id、original_path。
这些字段支持当前映射：

- 实际读取 `.flac` 成员。非英语样例的 metadata.file 使用旧 `.opus` 名称，这是上游保留字段，
  HF 在线样例也有同样情况；不能拿它代替真实 FLAC 成员路径。
- 英语样例使用 UUID 样式 id 和 book_id，其余七种语言使用复合 id、chapter_id 和 file。
  当前 source_key 使用语言 + 上游 id；完整 metadata 均保留，未假定所有语言共享相同 ID 格式。
- speaker 使用数据集 + 语言 + speaker_id，防止把未经确认的跨语言同号自动合并；
  这是保守的命名范围，不表示已验证跨语言同号一定是不同人。
- recording/group 使用原始录音 URL 的指纹。book_id/chapter_id 原样保存，不凭字段名推断它就是音频文件名。
- begin_time/end_time 是原始长录音上的秒数。当前 FLAC 已是片段，不按这些秒数再次裁剪；
  `segment_start_frame/end_frame` 保持空，原字段留在 metadata。
- dev 映射为上游 HF 的 valid，原 archive split=dev 另外保留；统一输出仍为 train。

2,626 个样例的实际时长与 end-begin 差异不超过一个原生采样帧；另两例最大差异约 3.125 ms。
来源秒数不能替代输出精确帧数，当前 adapter 使用实际 FLAC 头的 num_frames，这一处理应保留。
此次未证明秒数与每条音频声学对齐，也未检查全部 tar 的所有成员；全量接入仍执行完整配对、输出回读与身份检查。
HF 数据卡允许 metadata 可选，而当前监督转换要求音频/metadata 完整配对；若后续遇到缺失会明确失败，
不能把这次每包第一条全部有 metadata 推广为全库保证。

结论：此前列出的 MLS 文件名、时间字段和 speaker 范围没有发现需要阻止接入的映射错误。
原始清单已完整核对；样例映射已验证。未声称全库音频/文本质量验收完成。

保留结果：`reports/source-check/mls-source-check.json`。一次性首条探查脚本与下载副本已清理；
当前 bulk 在计划阶段直接核对本地 paths.yaml 与实际 archive 集合，原始数据未改动。


## 后续完整性补充：一个上游包本身截断

后续 Lance v0.1 单文件全量验收发现 `english/00128-of-00128/train-00000.tar.gz` 的 gzip/tar 尾部不完整。
该文件大小和完整 SHA256 与当前 HF LFS 元数据均相同，属于上游当前对象问题，不是本地传输缺失。
此前本报告比较的是 paths.yaml 的哈希及每包第一条，未检验所有包尾部；这两项结果不矛盾。
文件大小 67,010,560 bytes；完整 SHA256：
`8081d95d30063cef6c26fcfcb37b9bbe649461728557a9ce1ec9e7b6928d41bb`。
完整读取到 214 个成员（107 对），随后 tar/gzip 尾部校验失败，未发布该次转换。
用户已授权排除整包，规则固定于 configs/source-exclusions/mls_sidon-v0.1.json；
真实只读计划选入 2,627 包、排除 1 包，未知其他错误仍失败。
证据保留于 reports/lance-v0.1/ 的 mls-source-failure.json、mls-source-impact.json、mls-exclusion-plan.json。
