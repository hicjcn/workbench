# 源码副本性能与阶段计时

“创建独立只读预检副本”包括源码读取、Git 导出、独立 Git 初始化及当前改动同步。多个仓库会依次处理；Windows 的文件元数据操作、Git 子进程启动和磁盘性能会影响这一段的耗时。

## 减少重复工作

| 操作 | 调整 |
| --- | --- |
| 多仓库稳定快照 | 保留两次全局采集并比较完整内容和 HEAD，去掉每次全局采集内部再次双读子仓库的嵌套工作 |
| 合并子仓库基线 | 使用刚刚验证完成的独立子仓库基线；组合后再读取目标内容核对，随后检查来源快照，避免重复构建同一基线 |
| 同步工作区改动 | 已导出的未变化文件直接保留，只有内容或可执行位变化、新增和删除的文件需要处理 |
| Windows 可执行位 | 通过 NUL 分隔的标准输入批量交给 Git，避免每个文件单独启动进程；保留空格、中文和以短横线开头的路径 |

文件内容依然复制到独立目录，Git 仓库独立初始化。凭据排除、HEAD/内容变化检测、容量限制和跨盘复制后的内容校验继续执行。判断文件是否变化使用内容摘要；文件大小和修改时间相同，也会识别内容变化。`.harness` 保持原有规则：复制文件或已提交链接的原始文本，不展开链接目标。

## 定位长耗时

在 `workbench.toml` 已有 `[workbench]` 中设置 `log_mode = "detailed"`，重启后重跑预检。根据同一 `job_id` 查看当前实例的 `runner.log`：

| `step` | 内容 |
| --- | --- |
| `source.multi_snapshot` / `source.input_snapshot` | 多仓库来源校验及当前文件清单 |
| `source.repository` | 当前子仓库及其序号 |
| `source.archive` | 从固定 Git 提交导出源码 |
| `source.copy` | 写入已提交源码文件 |
| `source.git_init` | 初始化独立 Git、添加文件并提交基线 |
| `source.baseline` | 校验已提交基线 |
| `source.overlay` | 同步变化文件并再次核对来源 |
| `source.overlay_count` | 本次覆盖数量 `count` 与当前文件数量 `total` |
| `source.final_snapshot` / `source.verify_copy` | 核对单仓库或组合后的副本 |

各计时段输出开始、完成及毫秒单位的 `duration_ms`。这样可以进一步区分是 Git、文件写入还是快照读取较慢。更新后可直接重跑现有需求预检；源码副本优化也用于分析和评审等独立副本创建。

## 复现性能对比

样例构造 3 个仓库，每仓库包含 500 个合成业务文件，另有脚本、原样 `.harness` 文件、修改、删除和未跟踪文件。测量仅覆盖从已准备工作区创建独立只读副本的阶段；额外验证内容摘要一致、来源未修改、Git 无远程和凭据未导出。

Windows 11 示例（输出目录须尚不存在）：

```powershell
.\.venv\Scripts\python.exe poc\benchmark_source_copy.py --output .local-data\copy-benchmark --baseline-ref e5fba8b7a43c0e17e08cad60179cbc56269047f2 --repeat 2
```

基准 SHA 必须已存在于本地 Git 历史。省略 `--baseline-ref` 时只测当前版本。脚本从指定提交导出基准模块到独立目录，并分别创建合成测试数据，不切换业务源码或启动 Docker/模型。输出目录保留报告和样例，可在测试结束后按需移除该目录。

本次 Linux 样例中，副本创建的子仓库文件扫描读取从 **33132 次降到 16566 次**，覆盖复制从 **1506 次降到 6 次**。原始样本和耗时中位数见[性能验收记录](evidence/source-copy-performance-report.json)。这些数值衡量合成样例；用户 Windows 项目的实际耗时应以更新后的阶段日志为准。

回归用例额外覆盖复制期间来源变化、相同大小/时间但内容不同、HEAD 变化、跨盘目标损坏、容量超限，以及 Windows 批量模式位处理。运行完整检查使用 `python poc/run_unit_tests.py`。
