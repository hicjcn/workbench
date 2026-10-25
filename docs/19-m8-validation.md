# M8 应用服务验证

日期：2026-10-25。PR #7 已合并到 main，提交 `3d51b5f27ba56e4b9d7e811475ef68e438df0722`。本轮版本 0.8.0，SQL schema 仍为 `0006`，继续用样例完善接入。

| 检查 | 结果 |
| --- | --- |
| Python/API | 143 项通过，新增 19 项应用生命周期和异常边界验证 |
| Web | TypeScript/Vite 生产构建通过，按 React 最佳实践检查组件、表单标签、状态及清理 |
| 原有 Node 样例 | 2 项通过 |
| 浏览器/API/Runner/Docker/业务 PostgreSQL | 20 项真实演练通过，见 [应用证据](evidence/m8-application-report.json) |
| PostgreSQL 控制面 | 28 项通过，增加应用配置 JSON 在版本与恢复中的一致性，见 [控制面证据](evidence/m8-postgres-report.json) |

## 实际执行

在一个项目创建两个需求，每套同时运行开发容器、PostgreSQL 和应用，共六个真实容器。两个应用都使用容器端口 3000，不发布宿主端口；应用根文件系统及源码挂载只读，不挂载 Agent 认证目录，也未注入模型密钥或 OpenCode 服务密码。

两个 HTTP 服务实际查询各自 PostgreSQL，分别返回本需求的行数。A 的额外数据不出现在 B，跨需求数据库 IP 访问仍被阻止。检查通过 HTTP 状态、响应摘要和时间绑定记录，没有把数据库行或响应正文写入报告。实际 SQL 测试仍在开发容器中执行，不用 HTTP 健康检查替代业务测试。

单独停止应用后，开发环境和数据库保持可用，另一需求仍能响应。修改 A 的源码后 HTTP 检查失败并标记过期，明确停止、重启后才重新通过。B 的样例启动进程被改为退出码 23，启动任务确实失败并保留退出现场；恢复源码、明确停止与重启后恢复。上述已知应用失败不把仍运行的开发环境误标为停止。

丢失 A 应用、开发及数据库的运行记录后，按归属标签和挂载核实孤儿，停止所有写入者后保存数据库快照。重启后原有数据正确。浏览器保存应用配置、提交真实 HTTP 检查并观察成功；桌面和 390 px 手机布局无横向溢出，未发现页面脚本错误。

离线 v2 备份包含两套数据库 dump、应用检查历史和任务记录，排除应用运行记录、数据库认证及容器原始日志。恢复到全新目录后，历史报告显示已停止，没有容器或任务自动运行。明确启动后使用新的数据卷和认证，两个应用重新查询到正确的独立数据集。另一个实际 M7 v2 归档也恢复到新目录，摘要和未重放记录已核对。公开报告按实际 manifest 更正了初版脚本固定写成“M6 v1”的旧备份检查名称；没有把 v2 恢复误称为本轮 v1 实测。

首次真实演练发现 Docker 本地日志驱动的默认压缩与单文件轮转冲突，已显式关闭压缩并重新验证。随后六容器演练触发当前云环境 vfs 的磁盘空间限制，停止并核实资源后，清理了可再生成的样例 npm 下载缓存，保留源码、会话、快照和报告，再完成最终演练。没有更换 Docker 存储驱动或执行全局 prune。

新增受控测试覆盖配置校验与版本固定、预检门禁、幂等、容量拒绝、代码过期、进程退出、Docker 检查超时/容器缺失进入 UNKNOWN、启动响应丢失不重放、归属/只读挂载/镜像/端口核实、先停写入者再采集、取消清理、日志密码脱敏、备份拒绝活动记录及恢复历史。HTTP 探针另通过真实本地 HTTP 服务验证不跟随重定向、响应大小限制和不保存正文。异常测试的受控记录不充当业务或模型结果。

## 复现与边界

```bash
.venv/bin/python -m unittest discover -s tests -v
npm ci --prefix web
npm run --prefix web build
node --test poc/sample-project/server.test.mjs
.venv/bin/python poc/verify_dependencies.py --application \
  --root .local-data/m8-qa-new \
  --legacy-backup /private/path/to/previous-backup.zip
.venv/bin/python poc/verify_postgres.py --root .local-data/m8-postgres-new
```

需要固定工具基础镜像、PostgreSQL 17、浏览器依赖、Chromium 和足够的 Docker 磁盘空间。脚本给 API/Runner 一致设置容器上限 6。省略旧备份参数时，不声称验证了旧归档。原始数据、截图和日志保存在本地 `.local-data/m8-qa-04/` 与 `.local-data/m8-postgres-01/`，不提交公开仓库。

本轮没有调用在线模型，没有执行用户业务项目、服务间联调或压测。PostgreSQL 控制面脚本的 M3/M4 前置仍为明确标记的受控记录。当前只支持一个应用命令及一个容器内 GET 检查，没有宿主端口预览、持续健康监控或自动恢复进程；真实业务和在线 Superpowers/多模型完整流程仍待验收。
