# M9 跨项目全景分析验证

日期：2026-10-25。PR #8 已合并到 main，提交 `62e47a2bb304e496c067e023924412e84784b22c`。版本 0.9.0，控制面 schema 仍为 `0006`，没有新 SQL 迁移。

| 检查 | 结果 |
| --- | --- |
| Python/API | 162 项通过，新增 19 项全景分析与取消竞态验证 |
| 前端 | 锁文件安装、TypeScript 和 Vite 生产构建通过，按 React 最佳实践检查组件与表单 |
| 原有 Node 样例 | 2 项通过 |
| 浏览器/API/Runner/Docker | 9 项真实多仓库只读环境检查通过，见 [环境证据](evidence/m9-panorama-report.json) |
| PostgreSQL 控制面 | 31 项通过，新增全景请求并发幂等、环境与分析记录区分和历史恢复；见 [控制面证据](evidence/m9-postgres-report.json) |

## 真实执行的范围

创建三个独立 Git 仓库和需求，包括两个必要成员及一个跳过的候选成员。API/Runner 容器上限设为 1，浏览器提交全景环境检查，同一个实际分析容器只读查看三个项目，逐目录读取及摘要样本核对成功，写入尝试被拒绝。

样例源仓库中已跟踪的 `.env` 和 `credentials/token` 没有复制到候选副本。只创建一个独立 OpenCode 主目录，额外目录权限精确限制到三个候选根路径，其他工具默认拒绝。没有继承开发配置或创建数据库。完成后分析容器和网络清理完毕，三个原始仓库的代码摘要保持一致。

页面明确显示“模型未调用”，没有联合分析报告，正式范围仍不可确认。桌面和 390 px 手机页面无横向溢出，无浏览器脚本错误。第二次在真实运行中请求取消，确认任务为 CANCELED，全部候选预留解除，环境停止，历史没有新增结论。

首次演练发现检查完成与心跳读取取消之间的竞态：取消已接受但结果仍可能被发布。修复后在完成事务中再次检查取消标记；清理状态明确才取消并释放预留，清理不明仍为 UNKNOWN。最终演练与专门的受控竞态测试通过，没有将未取消成功的结果计入通过。

离线备份恢复到全新目录后，原环境检查历史和摘要保留，没有模型报告、自动范围决定或自动运行容器。另一个实际 M8 v2 归档也成功恢复，核对原归档摘要与未重放记录。完整数据与令牌只保留在本地，公开报告仅含结论和摘要。

## 受控验证与未验证部分

新增单元/API 测试使用明确标记的受控报告，验证逐项目覆盖、证据路径限制、候选预留与幂等、过期输入拒绝、需求变化、环境结果不能冒充分析、范围/联调/交付失效、正式交付历史，以及清理未知时全组阻塞。它们没有实际调用模型。

PostgreSQL 真实控制数据库中，4 路相同全景请求只创建一个任务并预留全部候选，受控环境结果只生成环境记录，备份到新 SQLite 后历史一致。该脚本的前置业务评审、测试与全景结果均是受控登记；不把它称作真实模型或业务测试。

本轮没有在线模型推理，没有验证联合分析报告的语义质量或模型实际跨目录工具调用，也没有接入用户业务仓库、自动发现候选项目、跨服务联调或压测。实际验证的是 OpenCode 配置被接受、代码可见性、Docker 只读隔离、任务生命周期与恢复；在线 Superpowers/多模型完整流程仍待验收。

## 复现

```bash
.venv/bin/python -m unittest discover -s tests -v
npm ci --prefix web
npm run --prefix web build
node --test poc/sample-project/server.test.mjs
.venv/bin/python poc/verify_panorama.py --root .local-data/m9-qa-new \
  --legacy-backup /private/path/to/m8-backup.zip
.venv/bin/python poc/verify_postgres.py --root .local-data/m9-postgres-new
```

需要 Docker、固定工具镜像 `agent-workbench-m1:local`、PostgreSQL 17 控制面镜像、浏览器依赖和 Chromium。省略旧备份参数时，不声称验证旧归档。原始验收目录是本地 `.local-data/m9-qa-02/` 和 `.local-data/m9-postgres-01/`，截图、私有备份及原始运行记录不提交公开仓库。
