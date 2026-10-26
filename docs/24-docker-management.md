# Docker 镜像维护与集中容器管理

本次补齐独立的「Docker 镜像」和「容器管理」导航入口。原来的「资源与恢复」继续提供资源汇总及离线备份恢复说明。

## 镜像维护

「Docker 镜像」显示 Runner 上报的本机镜像引用、完整 ID 和大小，支持搜索、明确标签的拉取、Dockerfile 版本保存与构建、未使用标签的非强制移除。镜像维护任务与需求任务分别记录，共享执行并发上限。API 不访问 Docker 套接字。

1. 点击「新建 Dockerfile」，填写名称、构建标签、Dockerfile、可选的构建上下文目录、登记人和原因。
2. 保存后只产生不可变版本，不会自动构建；历史 Dockerfile 可查看。
3. 在「Dockerfile 与版本」中点击「构建此版本」。目标必须是 `awb-user/` 开头的新标签，例如 `awb-user/java-dev:v1`；已有标签不会被构建覆盖。
4. 构建完成后到「项目登记」修订项目运行配置，将验证后的镜像用于后续新工作区。已有需求保留原来的固定配置与镜像 ID。

构建上下文目录填写 **Runner 所在主机的绝对目录**，例如 Windows 的 `C:\build\backend` 或 Linux 的 `/home/user/backend`；支持中文和空格。Runner 在执行时检查目录存在，再由 Docker 读取其中的文件，支持 `COPY`/`ADD` 并遵守目录中的 `.dockerignore`。API 只保存目录路径，不上传或归档目录内容。留空时仍只发送保存的 Dockerfile，适合仅安装开发依赖的镜像。

Dockerfile 始终采用页面保存的版本，即使上下文目录里另有 Dockerfile。上下文路径随版本固定，修改路径需保存新版本；已提交任务继续使用原版本的路径。**目录内容在执行构建时读取，保存版本不会冻结目录里的文件**。结果摘要记录实际使用的目录和生成的镜像 ID。目标标签已存在时仍拒绝覆盖。

例如把 `app.jar` 放在 `C:\build\backend`，在表单将该目录作为上下文，并保存：

```dockerfile
FROM eclipse-temurin:21-jre
COPY app.jar /app/app.jar
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
```

构建前使用 `.dockerignore` 排除不需要发送给 Docker 的文件；`.gitignore` 不会替代它。目录中若含有工作台配置或凭据，可使用以下规则：

```text
.git
.env
.env.*
workbench.toml
.workbench-run
.local-data
credentials
```

模型与 Docker 凭据仍由本机 Runner 环境配置。Dockerfile 与上下文不应包含密钥。暂不提供额外构建参数或注册表登录界面。启动脚本基础工具镜像的上下文仍由 `workbench.toml` 的 `launcher.tool_context` / `AWB_TOOL_CONTEXT` 配置，与这里每个 Dockerfile 版本的目录分开维护。

构建使用本机受管 Docker 守护进程及原有代理配置；环境提供 `CODEX_PROXY_CERT` 时以 `proxy_ca` 构建 secret 提供，Dockerfile 中的联网步骤仍需主动使用该只读 CA。不会禁用 TLS 或绕过网络策略。

维护记录显示排队、运行、完成、失败、待核实及人工结束核实。原始 Docker 构建/拉取输出可能含敏感数据，因此不持久化、不显示在页面；结果保留镜像 ID、引用和固定诊断摘要。当前不能从工作台查看逐行构建日志。

移除只针对用户明确选择的镜像引用，不强制删除、不 prune、不清理容器或数据卷。Runner 重新核对完整 ID，检查项目、历史运行配置的引用以及所有容器对镜像的使用，包括不属于工作台的容器。容器启动与移除共享主机准入锁；外部 Docker 操作仍可能改变宿主状态，需要刷新后处理。

## 容器管理

按需求集中查看 Agent 环境、应用服务、数据库状态，以及实际镜像、CPU/内存/PID 限制、工作区和只读配置挂载。提供创建工作区、环境预检、启动、停止、停止并核实、启动/停止应用、查看应用日志及执行记录。

这些按钮调用现有需求任务接口；不会直接 `docker start` 已丢失状态的容器。停止遵循应用退出、数据库快照和环境清理顺序，保留代码、Agent 数据及数据库数据卷。只读评审/分析副本展示在其他托管资源中，由所属任务管理。

页面只展示经现有控制目录归属规则筛选的容器，不提供任意宿主容器接管、任意命令终端或特权容器启动。

## 中断与恢复

镜像维护保存意图及幂等键后再交给 Runner，独立 owner/epoch/lease 禁止旧执行覆盖新状态。执行心跳中断变为 `UNKNOWN`，不重试构建、拉取或移除。仅尚在排队的任务可从页面取消；运行中任务等待 Docker 返回，最长 15 分钟。

操作超时或失联时，需要在 Docker 主机检查原操作是否结束、标签指向及受影响容器，再从页面「登记人工核实」。工作台要求旧主机锁已经释放，并保存人工核实人员与依据；该登记不会把原操作认定成功、不会自动重放。主机锁空闲不能证明 Docker 守护进程已停止所有后台构建，因此需要真实检查确认。

数据库迁移 `0007` 创建 `docker_recipes` 与 `docker_jobs`，`0009` 为 Dockerfile 版本新增可选 `context_dir`；旧记录默认留空，保持仅含 Dockerfile 的构建方式。离线备份 v2/0009 包含路径、Dockerfile 版本和维护历史，拒绝有活动/未知镜像任务的备份。兼容恢复既有 v1/0005、v1/0006、v2/0006、v2/0007、v2/0008 备份；旧记录自动补为空目录。外部构建上下文文件及镜像层不在工作台备份中；恢复后需重新确认目录、准备文件和镜像，不自动执行镜像任务或容器任务。

## Sites 试用与验证

同一个私有试用站点增加两个管理入口。示例镜像/容器有明确标记，Dockerfile 模板和历史保存在当前浏览器，并随试用 JSON 导出；已有试用需求保留。真实拉取、构建、移除和容器启动均提示未连接执行服务，不伪造成功任务。Sites 本身不能运行 Docker 和常驻 Runner。

验证：新增 13 项单元/API 测试；既有 187 项回归通过；实际 API/Runner/Docker 的 8 项检查及站点管理页 9 项浏览器检查通过。实际演练构建了继承既有工具镜像的环境、启动需求容器、从浏览器停止环境、拒绝移除使用中的镜像、移除未引用样例标签，并离线备份恢复模板和任务历史。M10 数据库副本从 0006 升级到 0007，原表数据摘要保持一致。

本轮没有执行在线注册表拉取或模型推理，未替代 M10 的完整在线开发与真实业务验收。证据见 [真实 Docker 检查](evidence/docker-management-report.json) 与 [站点交互检查](evidence/docker-management-site-report.json)。

复现命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
npm run --prefix web build
.venv/bin/python poc/verify_docker_management.py --root .local-data/docker-management-new
```
