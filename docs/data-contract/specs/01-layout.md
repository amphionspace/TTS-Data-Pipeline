# 01 · 目录与生命周期

## 根目录与发布单元

```text
DATA-TTS-UNIFIED/
├── README.md / CONTRACT.md / specs/ / schemas/ / examples/
├── datasets/
│   ├── libriheavy/
│   │   └── v0.1/
│   │       ├── manifest.json
│   │       ├── samples.lance/           # Lance 自己管理 data、versions、indices 等
│   │       ├── annotations/
│   │       │   └── alignment/<run_id>/{results.lance/,targets.lance/}
│   │       └── features/
│   │           ├── codec/<run_id>/{manifest.json,features.lance/}
│   │           ├── speaker_embedding/<run_id>/{manifest.json,features.lance/}
│   │           ├── text/<run_id>/{manifest.json,features.lance/}
│   │           └── merged/<run_id>/{manifest.json,features.lance/}
│   └── mls_sidon/v0.1/...
├── annotations/<task>/<run_id>/manifest.json  # 统一发布一个或多个dataset的任务输出
└── selections/<selection_id>/{manifest.json,rules.json,exclusions.jsonl,exclusion_changes.jsonl,duplicates.lance/}
```

示意中的 annotations/features 只在实际产生结果时创建。一对一标注在各samples.lance的
ann/<task>/<run_id>分支，分支与选择分支都从固定基础版本建立；全局任务manifest列出全部输出，见05。
一对多结果仍归所属dataset/release；历史sample_column只兼容读取。
原始来源名称与 canonical dataset_id 的映射固定在 adapter 中，例如 LibriTTS-R → libritts_r。
同一个 dataset 的基础、标注和特征都归属相同 release，跨数据集的annotation/selection分别由其全局manifest协调；训练组合由训练仓库管理。

## 物理分片

主表是一个 Lance dataset，不是一个巨大单文件，也不是每个任务一张表。
数据文件命名、fragment ID、版本清单、索引目录由 Lance 管理；不得手工重命名或按 glob 当作整张表读取。
新写入的目标文件大小为 1 GiB，采用 Lance `max_bytes_per_file` 的软限制；实际文件可能超出，
该阈值包含存储编码与元数据，不再声称等于 1 GiB 原始音频 bytes。
音频原有压缩保持不变，基础写入不额外启用通用 zstd；Lance 内部列编码不等同于音频重编码。
调整分片与 compaction 只影响物理布局，不改变业务 ID；新 snapshot 必须重新绑定训练定位信息。

## 发布和工作目录

基础接入先写 `datasets/<dataset_id>/v0.1.incomplete/`，所有验证和索引完成后原子改名为 `v0.1/`。
一个 release 的 manifest 初次只固定基础 snapshot，后续标注/特征各有自己的不可变 manifest。
annotation/selection 在 samples.lance 的独立分支增列，分支文件由引擎管理。
已有 main、固定版本与已发布 manifest 不变；训练端消费固定快照，不在统一源表上写训练专用列。
已发布的不可变边界是固定逻辑快照，不是整个目录的字节集合。分支内部文件交给引擎管理，不能手工搬动。
来源集合增加或基础映射修正需要另一个显式批准的 release；派生任务更新只增加 run。

恢复控制文件位于 `datasets/<dataset_id>/.state/v0.1/`，只包含计划、检查点与状态；身份审计临时 SQLite 放本地 TMPDIR。
这是工作状态，不是公开数据，也不是训练依赖。stdout 日志、性能报告、问题清单保存在 pipeline 的 reports/docs。
可以在数据集目录外另置工作状态，但必须保证程序记录实际位置；当前入口按上述默认规则。

普通读取只接受 complete manifest 引用的 snapshot。不读取 incomplete、临时 batch 输出或未发布的最新版本。
清理之前按 manifest 依赖判断可达性；整个 `.lance` 目录不是可随意清理的缓存。

codec、speaker embedding 和 text 独立发布并保留；merged 为额外生成的合并表，见 [13](13-text-features.md)。
音频特征见 [06](06-codecs.md)，文本见 [13](13-text-features.md)。
feature 的 selection_branch 模式直接固定已发布 selection 的分支，不重复写 targets；任意额外子集仍固定 targets.lance，见 06。
下游消费者固定所需 manifest、表路径、分支和版本。被引用的快照及数据文件必须保留；
清理需同时检查数据端依赖和已登记的下游引用。分支、重复证据和保留规则见 12。
mel 不是统一层必备产物；冻结/在线 speaker encoder 的两条路径见 [11](11-speaker-embeddings.md)。
