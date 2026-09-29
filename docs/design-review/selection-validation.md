# Selection 分支验证与实施决定

2026-09-29，pylance 12.0.0 / Arrow 25 / Lance 文件格式2.2。
只读生产输入，在 `/tmp/tts-selection-validation` 写独立验证副本；未给 unified 创建分支、改 main 或运行特征提取。
脚本：scripts/benchmark_selection_branches.py；安全约束拒绝非 /tmp 输出路径。
合成规则：duration_seconds<3 → reason3001/flags1，否则0；仅为存储验证，不是实际训练规则或正式flags含义。

## 全量行数基准

| 输入 | 行数与复制范围 | merge增列 | add_columns增列 | merge峰值RSS | add_columns峰值RSS |
| --- | --- | ---: | ---: | ---: | ---: |
| LJSpeech | 13,100；包含全部音频、文本、哈希、ID、时长 | 0.018秒 | 0.006秒 | 261 MB | 249 MB |
| Emilia | 40,264,231；只投影完整ID和时长 | 47.493秒 | 0.532秒 | 13.803 GB | 0.780 GB |

时间是增列调用本身；含全量结果核验/基础文件SHA复核的进程总时长分别为：
LJSpeech merge3.519秒、add3.329秒；Emilia merge50.180秒、add3.158秒。
准备副本与建立ID索引另耗18.775秒/47.645秒，不计入增列。每种模式独立进程，RSS含导入和验证。
Emilia仅7条命中这个合成时长条件，新增分支约220KB，不能用这种高度可压缩结果外推真实原因列体积。
LJSpeech分支约13KB；基础复制供验证，生产分支不会复制本次验证准备阶段的整表。

核验覆盖：

- merge右表每批倒序，完整逐行比较实际结果，证明按键关联而非依赖扫描行序。
- add_columns显式read_columns，完整逐行比较reason/flags和筛选计数。
- 两种方法都保持main版本、基础数据文件SHA256不变；分支引用全部基础文件，没有复制音频文件。
- 原sample_id BTREE查询、固定branch/version读取与reason过滤在合成回归中验证。

结论：大表优先流式add_columns。按ID merge在此机器上可行，但峰值内存显著更大；不以小样本表现推断全库。
本测试没有计算真实全局重复、文本冲突，也没有做磁盘排除表连接；这些可能主导生产时间。
Emilia没有搬运全部音频，未证明生产共享盘写延迟；没有测GPU或训练端吞吐。

## 清理、失败与特征绑定

tests/test_selection_branches.py 在一次性小表验证：

1. main overwrite后清理旧版本，**不设置任何tag**，仅靠branch引用仍完整读出旧音频。
2. 分支增列后对旧snapshot设tag，再修改分支头并清理，旧版本仍保持原选择结果。
3. 带tag分支删除被Lance拒绝。应用仍必须保护manifest依赖，不允许先删tag绕过。
4. UDF第二批故障不会提交半套新schema，分支版本/原数据不变；重试成功。
5. 特征乱序且缺一条时按key建立nullable row locator。特征main被覆盖后，固定旧snapshot的take仍匹配目标；
   缺特征行不就绪，但原selection不变。

这验证的是同根branch，不推广为外部shallow clone的回收保证。
生产发布器、依赖扫描/安全回收、全局重复执行器、真实特征绑定和训练读取器尚未实现。

## 留存与复现

原始JSON留在被Git忽略的 reports/selection-validation/；summary.json SHA256：
`2ee098c94bbdd6338c4d694227b4ea7171dd69dc1a39cf558986b12c64984df4`。
输入manifest SHA、base文件SHA、行数、列投影、峰值RSS/运行时间随报告保存。
临时音频/元数据副本验收后清理；保留脚本、回归测试和本报告即可重做。

```bash
python scripts/benchmark_selection_branches.py prepare \
  --source /workspace/data/DATA-TTS-UNIFIED/datasets/ljspeech/v0.1 \
  --work /tmp/tts-selection-validation/ljspeech --with-audio
python scripts/benchmark_selection_branches.py merge --work /tmp/tts-selection-validation/ljspeech
python scripts/benchmark_selection_branches.py add_columns --work /tmp/tts-selection-validation/ljspeech
```

Emilia使用同样流程，source改为emilia，work另置，省略--with-audio。已有输出不覆盖，复测使用新work路径。
依据：[Lance branches](https://lance.org/guide/tags_and_branches/)、
[data evolution](https://lance.org/guide/data_evolution/)，最终以固定安装版的上述实验为准。
