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
│   │       │   ├── audio_quality/<run_id>/manifest.json
│   │       │   └── alignment/<run_id>/{manifest.json,results.lance/,targets.lance/}
│   │       ├── views/<run_id>/{manifest.json,views.lance/}
│   │       └── features/
│   │           ├── codec/<profile_id>/<run_id>/{manifest.json,features.lance/}
│   │           └── speaker_embedding/<profile_id>/<run_id>/{manifest.json,features.lance/}
│   └── mls_sidon/v0.1/...
├── builds/<build_id>/{manifest.json,recipe.json,records.lance/}
└── assets/<dataset_id>/<release_id>/{manifest.json,assets.lance/}
```

示意中的 annotations/views/features 只在实际产生结果时创建。标量标注 run 可仅有 manifest，
其结果列在 samples.lance 中；有 `results.lance` 的 run 则引用该独立表。manifest 明确 storage_kind。
原始来源名称与 canonical dataset_id 的映射固定在 adapter 中，例如 LibriTTS-R → libritts_r。
同一个 dataset 的基础、标注、视图和特征都归属相同 release，跨数据集组合在 builds 中表达。

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
追加标注列可使主表产生新 snapshot，但不能覆盖基础 manifest 指定的旧版本。
来源集合增加或基础映射修正需要另一个显式批准的 release；派生任务更新只增加 run。

恢复控制文件位于 `datasets/<dataset_id>/.state/v0.1/`，只包含计划、检查点与状态；身份审计临时 SQLite 放本地 TMPDIR。
这是工作状态，不是公开数据，也不是训练依赖。stdout 日志、性能报告、问题清单保存在 pipeline 的 reports/docs。
可以在数据集目录外另置工作状态，但必须保证程序记录实际位置；当前入口按上述默认规则。

普通读取只接受 complete manifest 引用的 snapshot。不读取 incomplete、临时 batch 输出或未发布的最新版本。
清理之前按 manifest 依赖判断可达性；整个 `.lance` 目录不是可随意清理的缓存。

codec 和 speaker embedding 独立 profile/run，具体发布和工作目录见 [06](06-codecs.md)。
子集 feature run 在 features.lance 同目录另有 targets.lance，由该 run manifest 的 selection.table 固定；全体选择无此表。
mel 不是统一层必备产物；冻结/在线 speaker encoder 的两条路径见 [11](11-speaker-embeddings.md)。
