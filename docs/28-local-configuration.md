# 统一配置与 Windows 本地启停

工作台的本机运行参数集中在 `workbench.toml`，API、Runner、维护命令和启动器使用同一套读取规则。SQLite 保存项目、需求、Agent CLI 版本及流程历史；配置文件提供系统参数和可选的新工作区 CLI 默认值。

## Windows 首次使用

准备 Python 3.11+（推荐 3.12）、Node.js 22.12+、Git for Windows 和已启动的 Docker Desktop。Docker Desktop 使用 WSL2 后端和 Linux 容器。源码、配置和数据放在本地 NTFS 用户目录，建议路径简短。

在仓库根目录的终端执行：

```powershell
Copy-Item workbench.example.toml workbench.toml
notepad workbench.toml
.\setup.cmd
.\start.cmd
```

`setup.cmd` 创建或使用配置指定的 venv，安装 `.[server]`、构建 Web、构建工具镜像。默认镜像已存在时复用；需要重新构建时使用 `setup.cmd --rebuild-image`。已有配置文件不会被覆盖，数据目录不会清空。首次使用没有复制文件时，setup 会创建默认 `workbench.toml`。

`start.cmd` 返回后两个服务在后台运行，成功就绪后打开浏览器。使用启动输出中的 `api_url`，在本机读取 `<data_dir>\access-token` 并输入登录表单。启动器只打印令牌路径对应的数据目录，不打印令牌值。

```powershell
.\status.cmd
.\stop.cmd
.\start.cmd --no-browser
```

建议在已有终端执行，以保留失败提示。CMD 入口直接调用 Python，无需修改 PowerShell 全局执行策略。习惯 PowerShell 脚本时也可以使用：

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1 -NoBrowser
.\scripts\status.ps1
.\scripts\stop.ps1
```

## 配置位置和优先级

**显式命令行参数 → 对应环境变量 → TOML 文件 → 内置默认值。**

- Python 入口默认读取当前目录的 `workbench.toml`；脚本从仓库根目录运行。`--config` / PowerShell `-ConfigPath` 指定其他文件，也可设置 `AWB_CONFIG_FILE`。
- 显式指定但不存在的配置文件会报错，避免静默使用其他配置。`--config -` 用于只读取进程环境和内置默认值。
- 配置文件中的相对路径相对于配置文件目录；环境变量中的相对路径相对于进程工作目录。Windows 路径建议用 TOML 单引号，例如 `'D:\workbench-data'`。
- `[environment]` 变量仅写入本次应用及其子进程。已有进程环境优先；脚本不修改 Windows 用户/系统环境变量，不执行配置中的 Shell 命令。
- 改动后用 `stop` / `start` 应用。启动实例保留原配置快照；即使随后改坏配置，`status` / `stop` 仍按已记录实例操作。

自定义文件示例：先复制示例并调整其中路径，再执行：

```powershell
.\setup.cmd --config "C:\private\workbench.toml"
.\start.cmd --config "C:\private\workbench.toml"
.\scripts\stop.ps1 -ConfigPath "C:\private\workbench.toml"
```

同一配置文件只启动一个实例；多个配置文件如需并存，应分别使用不同的数据目录、日志目录和 API 端口。

## 字段与环境变量

完整模板见 [workbench.example.toml](../workbench.example.toml)。环境变量名称通常为字段大写并加 `AWB_` 前缀：

| 配置字段 | 环境变量 | 说明 |
| --- | --- | --- |
| `workbench.data_dir` | `AWB_DATA_DIR` | SQLite、工作区与正式材料目录 |
| `workbench.use_sqlite` | `AWB_USE_SQLITE` | 示例为 `true`，固定使用数据目录中的 `workbench.db` |
| `workbench.database_url` | `AWB_DATABASE_URL` | 可选外部 SQLAlchemy URL；只有关闭 `use_sqlite` 才使用。`--sqlite` 始终优先 |
| `workbench.port` | `AWB_PORT` | API/页面端口，默认 8340 |
| `workbench.port_range_start` / `port_range_end` | `AWB_PORT_RANGE_START` / `AWB_PORT_RANGE_END` | 容器映射可用的宿主端口范围，含起止端口，默认 `20000–29999`；起止值须在 `1024–65535` 内 |
| `workbench.api_url` | `AWB_API_URL` | Runner 连接地址；建议留空，自动跟随 port，仅允许本机回环 HTTP 根地址 |
| `workbench.model` | `AWB_MODEL` | 开发/分析/材料模型；仍遵循现有免费模型规则 |
| `workbench.web_dir` | `AWB_WEB_DIR` | 已构建的前端目录，默认 `web/dist` |
| `workbench.log_mode` | `AWB_LOG_MODE` | `concise`（默认精简）或 `detailed`（详细）；重启后应用到启动器、API、Runner 和新增任务事件 |
| `workbench.max_active_jobs` / `max_containers` | `AWB_MAX_ACTIVE_JOBS` / `AWB_MAX_CONTAINERS` | 全局任务与容器配额 |
| `workbench.lease_seconds` | `AWB_LEASE_SECONDS` | 任务租约；心跳/过期核实机制继续生效 |
| `workbench.task_timeout_seconds` | `AWB_TASK_TIMEOUT_SECONDS` | 内置 CLI 的任务超时；兼容旧 `AWB_TASK_TIMEOUT`，新名称优先 |
| `workbench.proxy_ca` | `AWB_PROXY_CA` | Agent 容器/模型诊断使用的 CA 文件；兼容已有 `CODEX_PROXY_CERT` |
| `workbench.docker_endpoint` | `AWB_DOCKER_ENDPOINT` | 留空按系统选择本机 socket/管道；Windows 允许本机 Desktop Linux engine / docker_engine，不接受远程 TCP |
| `launcher.venv_dir` / `logs_dir` | `AWB_VENV_DIR` / `AWB_LOGS_DIR` | 虚拟环境和日志目录 |
| `launcher.tool_context` / `tool_image` | `AWB_TOOL_CONTEXT` / `AWB_TOOL_IMAGE` | setup 基础工具镜像的构建上下文与标签；tool_image 同时作为新登记项目的默认镜像。页面维护的 Dockerfile 在其版本表单中单独配置上下文目录 |
| `launcher.startup_timeout_seconds` / `stop_timeout_seconds` | `AWB_STARTUP_TIMEOUT_SECONDS` / `AWB_STOP_TIMEOUT_SECONDS` | 启动就绪等待和停止命令等待时间 |
| `launcher.graceful_timeout_seconds` | `AWB_GRACEFUL_TIMEOUT_SECONDS` | API 优雅退出期间等待请求完成的时间 |
| `launcher.image_build_timeout_seconds` | `AWB_IMAGE_BUILD_TIMEOUT_SECONDS` | setup 镜像构建超时 |
| `launcher.open_browser` | `AWB_OPEN_BROWSER` | 启动后打开页面；命令行 `--no-browser` 可关闭 |
| `launcher.require_docker` | `AWB_REQUIRE_DOCKER` | 默认检查本机 Linux engine；仅试管理界面时可设 `false` |
| `launcher.stop_managed_environments` | `AWB_STOP_MANAGED_ENVIRONMENTS` | 默认停止服务后核实并停止当前数据目录的受管环境；`false` 表示仅停止控制进程 |

布尔环境变量接受 `true/false`、`1/0`、`yes/no`。端口、配额、超时和配置字段经过验证；格式错误会直接报字段名，不输出文件中的值。

例如修改端口和模型：

```powershell
$env:AWB_PORT = "8341"
$env:AWB_MODEL = "opencode/big-pickle"
.\start.cmd
```

脚本会让 API 与 Runner 共享同一有效配置，避免两个终端各自设置时出现差异。

## 项目运行环境

在「项目登记」的项目卡片选择「修改登记」，可修改名称、源码目录、仓库选择、共享文件和默认镜像。已有工作区保留独立源码副本；修改源码登记时，需先处理活动或待核实任务，不能移除未创建工作区的需求已选择的仓库。

「项目运行配置」使用宽屏分栏表单，支持环境变量、端口映射以及文件/目录挂载。保存会追加配置版本，新工作区在准备任务入队时固定所选版本；已有需求继续使用原版本，直到显式同步。

已有工作区可在需求详情的「项目环境与预检」或「容器管理」点击「同步最新配置」：

1. 先停止环境，并完成、取消或核实当前任务。同步保留源码、Agent 会话和数据库数据，不重新创建工作区。
2. 同步任务入队时固定项目最新配置及 CLI 选择；项目显式指定 CLI 时使用该版本，否则采用当前系统默认 CLI。Runner 重新解析本机镜像标签，同配置版本也可再次同步以刷新镜像。原先未绑定运行配置的旧工作区也支持同步。
3. 同步完成后，旧预检结果清除，旧流程、自审与材料结论需要重新确认，历史记录保留。同步不会自动预检或启动；重新执行「环境预检」，通过后再「启动环境」。

| 运行配置字段 | 行为 |
| --- | --- |
| `environment` | 变量名到字符串值的字典，传入开发和应用容器。与本机 TOML 的 `[environment]` 分开保存；不接受 `PATH`、`HOME`、`PORT` 等工作台保留变量 |
| `port_mappings` | 每项包含 `container_port`、`host_port`、`protocol`（`tcp` / `udp`）和 `service`（`agent` / `application`）。`host_port` 留空或为 `null` 时自动分配，指定时必须在上述端口范围内；启动时检查端口占用，只发布到 `127.0.0.1` |
| `volume_mounts` | 每项包含本机绝对路径 `source`、容器绝对路径 `target` 和 `read_only`，支持现存文件或目录。默认只读；设为 `false` 后，容器修改会写回宿主机 |
| `mounts` | 原有的只读配置文件副本：准备工作区时复制并固定摘要，目标位于 `/run/workbench-config/`，不修改原文件 |

应用端口映射需要先启用应用服务。直接挂载不能覆盖工作台或系统保留路径；只读分析角色的直接挂载会强制只读。项目环境变量的值会随运行配置保存，模型/CLI 凭据仍通过下节的本机私有配置或既有进程环境提供。

## 凭据、代理与 Agent CLI

本机私有文件可以填写：

```toml
[environment]
# 使用自己的既有凭据，示例不提供真实值。
# TEAM_API_KEY = "..."
# AGENT_CONFIG = "/run/team-agent/config.json"
```

也可在启动前设置对应环境变量；已有环境变量优先。允许模型/CLI 的 `_API_KEY`、`_TOKEN`、`_BASE_URL` 或 `AGENT_` 变量，以及 HTTP 代理/CA 变量；不接受 `PATH`、Python/Docker 控制变量或通过 environment 覆盖 AWB 运行参数。CLI 在 Linux 容器中运行，`AGENT_CONFIG` 这类路径应填写容器路径，并先在项目运行配置中登记相应只读挂载。

HTTP 代理可在该表设置 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`。宿主工具的 CA 可使用 `PIP_CERT`、`NODE_EXTRA_CA_CERTS`、`SSL_CERT_FILE`、`GIT_SSL_CAINFO`、`REQUESTS_CA_BUNDLE`，建议填绝对路径。Docker Desktop 守护进程的代理/网络仍由其自身设置管理。保持 TLS 校验开启。

可选 `[agent_cli]` 预置支持：

```toml
[agent_cli]
enabled = true
name = "团队 OpenCode 封装"
executable = "team-code"
expected_version = ""
timeout_seconds = 300
credential_env = ["TEAM_API_KEY"]
```

程序需要已安装在所选 Linux 镜像中。默认按程序名生成兼容 `serve/run/--version` 命令，也可显式填写 `serve_command`、`run_command`、`version_command`。环境覆盖使用 `AWB_AGENT_CLI_<字段大写>`；`AWB_AGENT_CLI_CREDENTIAL_ENV` 是逗号分隔的变量名。

启用后，启动时选择匹配的不可变版本作为新工作区默认；内容变更追加新版本，重复启动不会重复创建相同版本。已经固定的项目/工作区不会自动改变，可按上述步骤显式同步；项目指定了具体 CLI 版本时，需先修订项目运行配置中的选择。活动/未知镜像任务阻止变更时，启动输出显示 warning，页面仍可用于核实，核实后重启应用预置。关闭该选项后，默认 CLI 可通过现有管理界面维护。

真实 `workbench.toml` 和 `.workbench-run` 已被 Git 忽略，也从源码快照排除。配置里如果有凭据，请保存在个人专用目录，并使用 Windows 文件夹 ACL 限制其他用户访问；自定义命名的配置不要放进业务源码仓库。

## 启停、日志和恢复

启动器通过私有控制端口和随机实例标识记录自己创建的进程，校验 PID、创建时间和命令身份；就绪检查还核对实际 API/Runner 身份。重复 start 返回原实例。端口被其他程序占用会失败，不会接管该程序。

停止先通知 Runner。普通任务进入现有取消/核实流程，只读 Docker 状态查询可以中止；镜像构建等不可直接取消的操作等待结束。随后 API 退出，再按配置执行离线受管资源停止。超出等待时间返回 `STOPPING`，并显示正在等待的服务，保留状态，稍后查询或再次 stop。不会强杀其他 Python 进程、全局清理 Docker 或删除数据。

日志位于 `<logs_dir>/launcher.log` 和 `<logs_dir>/<instance>/api.log`、`runner.log`。配置旁的 `.workbench-run/<配置路径摘要>/instance.json` 保存自动生成的控制令牌、PID 和状态，不需要手改，也不要提交或共享。

默认 `log_mode="concise"`，输出启动/停止、任务阶段和结果；设置 `log_mode="detailed"` 增加 HTTP 路由状态、逐仓库复制、Docker 操作耗时、等待续期和失败代码位置。详细模式保持命令参数、认证值和模型输出的现有保护，不把请求体、提示词、环境变量内容或构建输出写入进程日志。页面已有的 Agent 输出和验证结果仍保留。配置示例和 Windows 查看命令见[日志模式与执行进度](33-progress-logging.md)。

启动器异常退出后状态显示 `STALE`；stop 会按原实例标记请求旧子进程退出，再取得排他维护锁核实资源。外部数据库配置若已经变化，需恢复原连接配置后核实；SQLite 按原数据目录处理。未核实的镜像任务仍需原有人工核实流程。状态为 `NEEDS_ATTENTION` 时先查看日志和页面记录，脚本不把该状态登记为成功。

安装/升级前先 stop，再重新 setup；setup 使用排他锁防止运行中安装依赖。仅查看页面时可以设置 `require_docker=false`、`stop_managed_environments=false` 并使用 `setup.cmd --skip-image-build`，此时实际 Agent 执行仍需要另行准备 Docker 和镜像。

## 验证范围

测试覆盖配置优先级、UTF-8/路径、凭据省略、CLI 版本预置、真实 API/Runner 后台启停、重复操作、配置改坏后的停止、端口冲突和 PID 复用保护。Windows CI 额外运行真正的 CMD 与 Windows PowerShell 脚本，在带中文/空格的目录中创建全新 venv、安装依赖、构建页面并准备 SQLite 工作区；不调用模型或真实 Docker。

Linux 已使用现有工具镜像实际验证：准备工作区、启动 Agent 服务（不调用模型）、停止并移除本实例容器、保留其他控制目录的容器及 SQLite/代码，再次停止。Windows 控制进程检查由上述 CI 执行；Docker Desktop 的实际容器启停仍需在目标 Windows 11 主机联验。
