# 核查与待办

当前方案为 Lance v0.1，共 13 个 adapter；全量转换已启动，Emilia2 暂不接入。
规范见 [data-contract](../data-contract/README.md)，21 个来源的接入映射见 [datasets](../datasets.md)。

| 文档 | 用途 |
| --- | --- |
| [验收与契约复核](adapter-contract-review.md) | 原四个来源与新增九个来源的验证范围、契约修正和性能边界 |
| [LibriHeavy / MLS 核查](libriheavy-mls-check.md) | 配置交集、来源清单、MLS 坏包与明确排除 |
| [Emilia / YODAS 本地核查](emilia-local-check.md) | 4,343 包首条检查、采样解码、字段映射与训练注意事项 |
| [当前问题](issues.md) | 待实现的标注、视图、codec、训练构建及其他来源问题 |

reports 仅保留来源核查、验收和排除依据，已忽略 Git。
早期格式选型草案、阶段日志、旧目录盘点快照和一次性探查脚本已清理。
真实样本预览与完整转换通过不代表音文一致性、音色身份和训练吞吐已验证。
