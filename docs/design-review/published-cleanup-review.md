# 已发布数据清理核验 · 2026-09-29

本记录描述已完成的维护操作，不是待执行的规划。2026-09-29 再次扫描确认所有已发布 release 的 samples.lance/data/.tmp* 均为空，MLS 旧 identity.sqlite 不存在。

## 已完成清理

| 数据集 | 孤儿临时文件数 | 字节 |
| --- | ---: | ---: |
| aishell3 | 1 | 163,450,880 |
| galgame | 102 | 52,063,248,623 |
| libriheavy | 69 | 39,351,936,327 |
| libritts_r | 1 | 746,330,944 |
| mls_sidon | 56 | 45,879,961,495 |
| starrail_voice | 11 | 4,942,754,552 |
| vctk | 1 | 518,819,840 |
| wenetspeech4tts | 23 | 5,628,331,615 |
| wutheringwaves | 4 | 1,410,650,112 |

共删除 268 个 `.tmp*`，150,705,484,388 字节（150.705 GB）；另经明确授权删除 MLS `.state/v0.1/identity.sqlite`，3,109,679,104 字节。合计 153,815,163,492 字节。

执行前持有各 release 的转换锁、核对无 incomplete 转换，并检查候选文件未被进程打开。临时文件逐一对照全部保留 Lance 版本，而不只对照最新版本。
执行后再次核对 16 个数据集的 manifest SHA256、行数、版本列表及各保留版本的数据文件引用，均通过。没有改写已发布 manifest 或删除合法分片。

## 清单与证据

逐文件清单保存在本地被 Git 忽略的 reports/maintenance；以下摘要将执行证据绑定到本记录：

- `reports/maintenance/orphan-cleanup.json`：SHA256 `5885ad5b807652c66bbab2b0fcdd7dc2e9e6150b75297ec26c3af32148f13321`。
- `reports/maintenance/mls-sqlite-cleanup.json`：SHA256 `e9fa57dfcef9a568ad03a027d2ac0202991867124c9527d8b6cc29aeaa87bd55`。
- `reports/maintenance/temporary-files-inventory.json`：SHA256 `8bacfc997850f0b6fcc16b361fa0145e6f5aaef604e18f0e1b5aa26f9518b5b7`。

这些本地报告不是训练依赖；部署或归档仓库时如需逐文件审计，须同时保留报告。

## 防止再次遗漏

finalize 的孤儿清理同时覆盖 `.lance` 与 `.tmp*`，保留被引用文件、目录和符号链接。
新清理在 unlink 前持久化 planned 事件，完成后持久化 removed；事件包含文件名、bytes 和带时区时间，同时打印到 stdout。
工作状态 cleanup.jsonl 跨 finalize 重试保留，最终写入 finalization.cleanup；只把 removed 计入确认删除数量。planned 无对应 removed 时结果未确认。
已发布 release 的清理是本次单独执行的维护操作；不会依靠未来 finalize 自动清理，也不为补审计字段改写历史 manifest。
