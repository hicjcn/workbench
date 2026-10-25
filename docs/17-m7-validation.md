# M7 PostgreSQL 隔离与恢复验证

日期：2026-10-25。PR #6 已合并，提交 `935a84a4aa0fd2b9993df94b157d0d0ada826c9f`。本轮按用户选择继续使用样例完善接入，版本 0.7.0，控制面 SQL schema 仍为 `0006`。

| 验证 | 结果 |
| --- | --- |
| Python/API | 124 项通过，新增 18 项，覆盖模板校验、整套容量、只读隔离、资源归属、密码脱敏、数据集/摘要校验、快照失败待核实、意外数据卷丢失、二进制流错误保密，以及恢复中断保留有效输入 |
| 前端 | TypeScript 和 Vite 构建通过 |
| 原有 Node 样例 | 2 项通过 |
| 浏览器、真实数据库及恢复 | 13 项通过，见 [依赖隔离证据](evidence/m7-dependencies-report.json) |
| PostgreSQL 控制面 | 27 项通过，增加依赖配置 JSON 在版本保存和恢复中的一致性；见 [控制面证据](evidence/m7-postgres-report.json) |

真实演练构建包含锁定客户端的样例镜像，在同一个项目中创建两个需求。两套环境同时运行 4 个容器，使用不同的 PostgreSQL 卷、网络及密码，数据库没有宿主端口。分别写入只属于 A/B 的标记，确认互不可见；从 A 开发容器直接访问 B 数据库 IP 的 TCP 连接被阻止。开发容器内的 Node 测试实际创建表、写入及读取通过。

独立预检没有创建或连接开发数据库，报告确认不含数据库认证环境变量和私有网络。代码与认证边界也有单元验证；本轮未调用在线分析或评审模型，不能把该结果称为多模型实测。

演练移除 A 的开发/依赖运行记录，证明可以核实孤儿归属、先停止写入者、采集快照并保留数据卷，再次启动后数据仍在。B 的数据库被外部停止后，明确核实会临时启动数据库完成快照，随后关闭，重新启动后数据保留。

v2 备份包含两个真实 `pg_dump` 文件，排除依赖认证与运行记录。恢复到全新目录后没有自动启动任务；明确启动后，两个数据集分别导入新卷，密码重新生成，A/B 标记正确且仍独立。旧目录的卷不被复用，原始代码未改变。实际 M6 v1 归档也完成了新目录恢复。

导入/导出使用二进制流，不依赖 Docker 对 tmpfs 文件的 `cp` 行为。失败演练发现这一兼容性问题时，数据库与数据卷均保留；修复后才得到最终通过报告。恢复失败、遗留写入无法终止等错误场景由明确标记的受控单元测试验证，不伪称实际破坏了业务数据库。

## 复现

```bash
.venv/bin/python -m unittest discover -s tests -v
npm ci --prefix web
npm run --prefix web build
node --test poc/sample-project/server.test.mjs
.venv/bin/python poc/verify_dependencies.py --root .local-data/m7-qa-new \
  --legacy-backup /private/path/to/m6-backup.zip
.venv/bin/python poc/verify_postgres.py --root .local-data/m7-postgres-new
```

需要 Docker、固定工具基础镜像、`postgres:17-bookworm`、浏览器依赖和 Chromium。脚本构建自己的镜像标签，测试结束时仅清理专用目录中确认归属的临时资源及数据卷，保留源码、快照和报告。生产停止逻辑不删除数据卷。省略 `--legacy-backup` 时不会声称检查了旧归档。

原始验收目录为本地 `.local-data/m7-qa-04/` 和 `.local-data/m7-postgres-01/`。完整备份、数据库、运行密码和配置内容不提交公开仓库；公开证据仅包含检查结论和摘要。

## 范围

本轮真实运行了样例数据库和 SQL 测试，没有生成模拟模型答复，没有调用模型，也没有接入私有业务项目。控制面脚本保留的 M3/M4 前置仍使用明确标记的受控记录。用户业务数据库、性能测试、在线 Superpowers 开发与多模型评审仍待验证。

当前只支持单机 PostgreSQL 17 模板；没有任意 Compose 编排、Redis/MQ、生产数据库接入、数据卷配额或网页删除数据卷。不能把样例隔离和恢复通过当作全部 MVP 验收完成，剩余事项仍见 [AC 覆盖表](13-m5-validation.md)。
