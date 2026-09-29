# TTS Data Pipeline

将原始音频转换为统一 **Lance** 数据，后续补标注、生成多个 codec 版本，再按固定配方构建训练输入。
发布版本 **v0.1**，统一根目录 `/workspace/data/DATA-TTS-UNIFIED`。15 个来源已适配；原 13 个来源的全量转换已启动，Emilia 两个来源加入转换，实时状态见 `python scripts/conversion_status.py`。

已有 15 个 adapter：CSEMOTIONS、LibriTTS-R、LibriHeavy、MLS SIDON、AISHELL-3、LJSpeech、
VCTK、HiFiTTS、WenetSpeech4TTS、genshin-voice、starrail-voice、Galgame、WutheringWaves-2.2、Emilia、Emilia-YODAS。
新增九个已完成小样本接入测试，未进行全量音频验收；Emilia2 暂不接入。
项目直接运行本地模块，无需 pip install -e .。旧 Parquet 输出及 converted 用法已取消，原始 Parquet 读取仍保留。

## 运行

```bash
conda activate tts-data
cd /workspace/workspace/yanglin/tts-data-pipeline
python scripts/tts_data.py --help
python scripts/conversion_status.py
pytest -q
ruff check .
ruff format --check .
```

全量转换命令和恢复方式见 [转换说明](docs/bulk-conversion.md)，这里的命令示例不表示任务正在运行。

## 发布布局与读取

```text
DATA-TTS-UNIFIED/datasets/<dataset_id>/v0.1/
├── manifest.json
└── samples.lance/
```

后续 annotations/views/features 位于同一 release 中。全量布局和全部契约见 [v0.1 contract](docs/data-contract/README.md)。
Lance 管理物理文件与 snapshot，文件目标约 1 GiB；不再手工按 Parquet glob 读取。
音频原始 bytes 内嵌，source_split 固定 train，原 split/config 保留于 metadata。

```python
import json
from pathlib import Path
import lance

release = Path("/workspace/data/DATA-TTS-UNIFIED/datasets/libriheavy/v0.1")
manifest = json.loads((release / "manifest.json").read_text())
assert manifest["status"] == "complete"
ds = lance.dataset(release / manifest["table_path"], version=manifest["lance_version"])
for batch in ds.to_batches(columns=["sample_id", "text", "language"], batch_size=8192):
    print(batch.num_rows)
```

转换校验原始文件哈希、每条音频 bytes/头部、完整回读与全局 ID；--deep-verify 额外完整解码。
标注/codec/训练执行器的实现边界见 [待办](docs/design-review/issues.md)，规范不代表这些执行器均已实现。

## 仓库导航

| 入口 | 内容 |
| --- | --- |
| [数据约定](docs/data-contract/README.md) | 唯一规范源稿；同步至统一根目录 |
| [转换](docs/bulk-conversion.md) | 并行写、检查点、发布、恢复 |
| [核查与待办](docs/design-review/README.md) | 当前验收证据与实现边界 |
| [LibriHeavy / MLS 复核](docs/design-review/libriheavy-mls-check.md) | 上游与本地核查 |
| [数据适配器](docs/datasets.md) | 已有和计划来源的映射规则 |
| [环境](docs/environment.md) | Conda、中科大源与依赖 |

src 保存转换逻辑；scripts 保存入口与探查工具；tests 保存回归检查。
reports、artifacts 和缓存被 Git 忽略。原始数据只读。
