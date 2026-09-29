# 09 · Lance 查询、提交与保留

## 读取与索引

读取始终使用 Lance dataset API 和固定 snapshot；Lance 的 fragment/data file 不能像 Parquet 那样 glob 拼接。
示意（路径/version 从 manifest 取得）：

```python
import lance

ds = lance.dataset(".../datasets/libriheavy/v0.1/samples.lance", version=2)
for batch in ds.to_batches(columns=["sample_id", "language", "duration_seconds"], batch_size=8192):
    process(batch)
```

版本 2 只是例子，不能在程序硬编码。
查询 metadata/quality 时不选择 audio；查询少数样本使用 sample_id 标量索引，音频按需读取。
批量全库任务使用顺序扫描；不要对数百万样本逐个发 SQL 查询。

samples 必有 sample_id BTREE；views 有 view_id 索引；独立结果表有 target_id 索引；codec/speaker embedding 有 target_id/feature_key 索引，view 特征还有 parent_sample_id 索引。
语言、speaker、quality 等索引由实际过滤模式选择；写了索引不代表所有 filter 都会使用它，应检查执行计划。
索引不检查唯一性、不执行外键，也不保证所有 merge 都不扫描。
用户给定 ID 必须先验证/安全构造过滤表达式；本 contract 的 sample_id 是 64 个小写十六进制字符（256 bit）。

## 选择分支读取

```python
# entry 来自校验过的 complete selection manifest；不得省略分支版本。
base = lance.dataset(root / entry["table_path"], version=entry["base_version"])
selected = base.checkout_version((entry["branch"], entry["lance_version"]))
for batch in selected.to_batches(
    columns=["sample_id", "text", "language"],
    filter="selection_reason = 0",
    batch_size=8192,
):
    process(batch)
```

同根分支继承文件，不采用外部 shallow clone。新增列不默认建索引：低基数 reason 的顺序筛选
先测过滤开销，必要时再建相应索引。分支完整发布约束见 [12](12-selections.md)。

## Annotation按位置组合

同bv的平级分支在完整验证基础列和有序sample_id一致后，可按同一逻辑行区间take，
或scan_in_order=True无过滤扫描并重组批次边界，再校验sample_id、应用筛选。
不得直接zip独立扫描器，不得各自过滤后拼接；_rowid不是take的逻辑行偏移。
多分支依赖和发布/清理约束见 [05](05-annotations.md)，未实现自动验证时不假定可以安全按位置组合。

## 写入协调

一个表的 schema 修改/提交由单一协调者执行。大量 worker 可并行算分数、编码和生成 fragment，
但不让每个 worker 各自给 samples 增列。提交绑定读取版本，冲突要复核后重试，不能自动覆盖新结果。
结果先写临时存储、全量验证后发布；提交成功但 manifest 尚未完成时，消费者仍不使用该结果。
中断恢复先判断 snapshot 是否已提交，避免重复增列或重复数据。

输入 snapshot 固定后，其他任务追加不相关列不应改变该任务的实际输入；提交时仍验证目标键和指纹。
新增标注列不能修改 27 个基础列；可用基础 revision、audio_sha256 和文件清单验证。

## 大二进制与数组

v0.1 基础 audio 使用 Arrow struct<bytes:large_binary,path:string>，直接存原始编码 bytes。
这是内嵌二进制列，不等于启用了 Lance 专门的 Blob 扩展 API；不能对该列直接假定 take_blobs 可用。
大长音频如需 Blob 格式，先验证 decoder、范围读取、版本兼容和迁移方案，再显式演进 storage profile。
不因引擎支持外部 URI 就默认依赖可能被删除的 raw 文件；发布自包含。
codec 使用 Arrow 嵌套整数数组；只读 codes 和缓存 embedding 时不需要加载原音频列。
在线 speaker encoder 路径按固定快照读取所选参考音频，frontend 实时提取，不要求预先保存 mel。

## 快照保留和清理

基础 manifest、selection、标注 run、feature run、build 和 training_plan 都是 snapshot 的保留根。可为这些版本创建 Lance tag，
但 tag 只是辅助，recipe 仍保存 table/branch/整数 snapshot 和 manifest 哈希。
基础与 selection 输出必须创建保护 tag；被引用版本的 tag 不得改指向或删除。
先收集 selections/*/manifest.json 及其他保留根，再检查引擎分支关系；禁止手工只扫 data/ 判断孤儿。
禁止对被引用版本执行 cleanup_old_versions；只保留最新版本会破坏训练复现。
子集 feature run 的 selection.table 引用的 targets.lance 与 features.lance 均须保留，不能将选择表视为临时文件。

清理流程：枚举全部 complete manifest → 收集被引用 snapshot → 检查无并发写/未完成任务 →
递归收集annotation间及selection引用的annotation依赖（已复制文本仍保留审计引用） →
确认可回收版本 → 调用受支持的引擎清理 API → 验证仍保留的版本可读。
不得手动删除 samples.lance/data 或 _versions 内“看起来旧”的文件。
compaction 是新的提交，会改变行位置和索引布局；旧 snapshot 可继续被 build 引用，直到解除引用并正式清理。
