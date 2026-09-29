# 首次正式 selection

规则文件为 configs/selections/first-v0.1.json，contract/release仍为v0.1。
输入为16个已发布基础快照，共134,832,658条；规范见 [12](data-contract/specs/12-selections.md)。

- 保留1–120秒（包含边界），文本有效、语言可识别，当前11种语言全部允许。
- text用Python str.strip去首尾空白；有变化才在分支写selected_text覆盖，source=0表示基础规范化。
  读取用`selected_text if selected_text is not None else text`，不能用`selected_text or text`。
- en-US/en-us→en、zh-CN/zh-cn→zh写入selected_language稀疏覆盖；未变化值为null并沿用base。
  不改变speaker_id，不自动裁掉其他地区/脚本子标签；筛选、比较、统计与训练使用相同选用语言。
- 完整64位十六进制audio_sha256全局分组，不使用旧64-bit前缀做最终判定。
  候选先通过基础规则，文本/语言仍冲突的组排除；无冲突按固定来源优先级、sample_id选唯一代表。
  来源优先级不表示质量评分，Emilia先于YODAS；未入选记录仍完整保留。
- 固定鸣潮32条一帧音频及Galgame7条已审阅长音文错配。相同坏音频hash合并为一个排除作用键；
  每项保存实际证据，和时长/缺文本等其他命中同时记录flags。
- 不依赖annotation、不生成codec、不做评估划分。音频存在性继承已发布基础接入的非空bytes/哈希/全量回读验收，
  这次重新核对元数据，不重新解码全部18TB；coverage明确这一范围。

## 执行阶段

```bash
python scripts/build_selection.py plan --work /tmp/tts-selection-<run> \
  --rules configs/selections/first-v0.1.json
python scripts/build_selection.py run --work /tmp/tts-selection-<run> --workers 256
```

plan只读基础数据并固定输入manifest哈希、快照、fragment分组、精确排除证据和规则。
run依次scan → deduplicate → publish；也可以显式运行各个phase恢复。
本执行器只支持首个独立selection，不支持annotation/新一轮排除继承；已存在完整selection时拒绝新建空继承根。

扫描按约25万行的fragment组拆分，256进程，每进程限制BLAS/LanceCPU线程为1，IO线程2。
大型来源内部并行；全局分组统一协调。最后不同dataset分支并行写入，最多16个协调者，同一表单写者。
正式执行使用冻结源码副本；修改仓库不能静默改变正在执行的规则。日志与进程号留pipeline的reports/selections。

局部有界扫描产生本地mmap检查点；随后构建完整hash全局排序索引和决策数组。
这些数组只在/tmp用于计算/恢复，不作为发布格式。估计行数组两份约47GB、排序索引约10GB，
另有重复表和校验临时开销；不是内存中的数亿Python对象，也不写共享盘SQLite逐条提交。
中断扫描复核片段文件SHA；重复阶段未完成则由原始检查点重新构建，不沿用部分被修改的原因码。
已提交分支可全量核验后复用；全体输出完成才原子发布selection manifest。

每条分支回读核对sample_id、reason、flags与最终文本覆盖；保护base/输出tag，main和基础文件引用不变。
分支的输入定位使用固定快照原生row address并校验sample_id，拒绝含删除行的base；不靠回调调用顺序。
重复表保存全体重复成员和代表。正式manifest记录语言条数/时长、规范化统计、完整规则/排除证据、版本和未检查项。

最终输出为selections/<selection_id>/与各samples.lance的同名分支，用户读取只依赖这些正式产物。
发布并完成回读后，本地数组、源码副本和扫描检查点可清理；运行中不可删除。
