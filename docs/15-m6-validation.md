# M6 样例项目接入验证

日期：2026-10-25。PR #5 已合并到 `main`，合并提交 `dc3f62c79b404cf3d7e4004716d7db058a050cd4`。按用户选择，本轮使用样例完善运行配置及预检，未接入私有业务项目。版本为 0.6.0，迁移为 `0006`。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| Python 单元及 API | 106 项通过，新增 11 项，覆盖配置并发前提、固定版本/镜像、独立私有副本、非法挂载、旧环境兼容、预检门禁、停止未知状态及配置备份恢复 |
| 页面构建 | TypeScript 和 Vite 生产构建通过 |
| 原有 Node 样例 | 2 项测试通过 |
| 自定义镜像、浏览器和实际运行 | 11 项检查通过，见 [接入演练证据](evidence/m6-onboarding-report.json) |
| PostgreSQL 17.11 | 26 项检查通过，包括 4 路配置首次保存只有一个版本、旧需求兼容及固定版本恢复；见 [PostgreSQL 证据](evidence/m6-postgres-report.json) |
| M5 SQLite 副本升级 | `0005 → 0006`，原 24 张表行数及内容摘要保持不变，见 [升级证据](evidence/m6-upgrade-report.json) |

浏览器在真实 API 和 Runner 中保存项目配置，创建工作区，执行独立只读预检，再启动需求环境。Docker 实际检查确认镜像 ID 固定、0.5 CPU、1536 MiB，以及配置文件挂载只读。自定义样例 Dockerfile 从固定工具基础镜像构建，沿用 OpenCode 1.19.4 和已锁定 Superpowers。

项目构建命令实际执行并读取配置副本。容器内执行的三个 Node 测试包括原有两个接口逻辑测试和新增的配置可读/写入拒绝检查，全部通过。修改主机配置文件并保存项目 v2 后，旧工作区仍用 v1 副本，复测通过；新工作区固定 v2，缺少命令时预检失败并阻止启动。原始源码与主机配置未被容器修改。移动页面没有横向溢出或浏览器运行错误。

离线备份和新目录恢复验证保留配置版本、绑定与源码，不包含配置文件副本内容。演练也实际读取 M5 生成的 `0005` 归档并恢复为当前架构，没有覆盖原数据或自动重放任务。镜像及配置内容需要用户单独保管，数据备份不是镜像和秘密文件备份。

本轮没有生成受控模型答复来代替推理，也没有调用模型。预检通过仅表示镜像工具、技能发现、挂载和入口检查通过；真实模型连通性、Superpowers 开发行为、用户业务测试及压测尚未验证。PostgreSQL 脚本保留的 M3/M4 前置仍为明确标记的受控记录，不能算在线开发验收。

## 复现

```bash
.venv/bin/python -m unittest discover -s tests -v
npm ci --prefix web
npm run --prefix web build
node --test poc/sample-project/server.test.mjs
.venv/bin/python poc/verify_onboarding.py --root .local-data/m6-qa-new \
  --m5-backup /private/path/to/m5-backup.zip
.venv/bin/python poc/verify_postgres.py --root .local-data/m6-postgres-new
.venv/bin/python poc/verify_upgrade.py --database /path/to/m5/workbench.db \
  --root .local-data/m6-upgrade-new
```

需要 Docker、固定基础镜像、PostgreSQL 镜像、浏览器依赖和 Chromium。`--m5-backup` 可省略；省略时不会声称验证了旧归档恢复。脚本只创建专属验收目录，清理自己的服务、容器及新建镜像标签，保留源码、数据库和报告。本地原始证据位于 `.local-data/m6-qa-02/`，不向公开仓库提交令牌、完整数据库或配置副本。

## 剩余工作

下一次可以按 [操作说明](14-m6-project-onboarding.md) 接入一个真实小需求，补齐用户技术栈工具、业务依赖和实际模型认证，完成完整开发、评审、主管设计审批及回归。当前还未实现网页构建 Dockerfile、Compose 依赖、任意可写宿主挂载、运行中需求配置迁移、自动业务联调或在线模型全程验收。原设计中的 P0/MVP 验收状态仍以 [AC 覆盖表](13-m5-validation.md) 的真实项目待办为准。
