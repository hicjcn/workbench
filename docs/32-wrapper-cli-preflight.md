# 封装 CLI 的镜像预检

预检以**当前需求已固定的 Agent CLI 配置**为准，不根据镜像标签猜测程序名。只提供 `team-code` 等 OpenCode 兼容封装的镜像可以通过预检，不必另建 `opencode` 命令别名。

## 配置与运行

在“Agent CLI 配置”维护实际镜像内的命令，例如：

```text
服务命令：team-code serve --hostname {host} --port {port}
任务命令：team-code run --attach {endpoint} --model {model} --format json
版本命令：team-code --version
```

版本命令退出码应为 0，并在 stdout 或 stderr 输出版本。精确版本留空时不强制使用 OpenCode 版本号；填写时与实际版本输出核对。

需求详情显示封装名称，说明已选择该配置。预检在独立只读副本继承同一版本；后续修改默认值或创建 CLI 配置新版，不会静默替换已准备需求的绑定。准备前在项目运行配置选择对应 CLI，或设为新工作区默认。已有需求可停止环境后同步最新配置，再重新预检。

工作台保留镜像的原有 `ENTRYPOINT` 和 `CMD`，先按 Dockerfile 启动容器，再通过 `docker exec` 动态启动需求已固定的 Agent CLI。Dockerfile 可以只启动 code-server，所选 CLI 无需写进镜像启动命令，也不需要随着 CLI 配置变更重建镜像；程序仍须预先安装在镜像中。镜像的默认服务需要持续运行，且不要同时启动另一个占用 `4096/tcp` 的 Agent 服务。

CLI 的 exec 命令使用 `/bin/bash` 包装器加载初始化文件。原命令及参数独立传递，不拼接为 Shell 字符串。预检报告显示实际 CLI 服务、任务和版本命令；Docker inspect 中的容器入口继续显示镜像自己的入口，例如 code-server。入口脚本仅导出的环境变量不会自动传给后续 exec；CLI 所需 PATH 应通过 Dockerfile 的 `ENV`、Bash 初始化文件或绝对路径配置。若 CLI 本身需要单独初始化，可使用镜像内的脚本作为服务命令，例如：

```text
/usr/local/bin/agent-entry.sh team-code serve --hostname {host} --port {port}
```

内置工具镜像 `poc/container/Dockerfile` 仅安装工具，默认使用 `sleep infinity` 保持容器运行，Agent 服务由工作台启动。旧版 `agent-workbench-m1:local` 的默认命令直接启动 OpenCode，升级后需重建一次基础镜像和基于它的项目镜像，再停止已有环境、同步最新配置并重新预检：

```bash
docker build -t agent-workbench-m1:local poc/container
docker build -t agent-workbench-node:local poc/onboarding
```

已有只启动 code-server 的项目镜像可以继续使用原入口。容器仍使用运行配置中的用户、独立 HOME、只读根文件系统和固定挂载；镜像的默认服务也需要兼容这些设置。

### root 安装的 CLI 与 Bash 初始化

如果 CLI、Node 或 NVM 安装在镜像 `/root` 下，在“项目运行配置”勾选“以 root 执行容器命令，并先加载 /root/.bashrc”（字段 `run_as_root`）。配置界面新建或未设置时默认勾选；已有旧版本未包含该字段时仍保留普通用户行为，需保存新版本后才能切换。取消勾选时使用原容器用户，加载其可读的 `HOME/.bashrc`。

镜像必须包含 `/bin/bash`。root 模式在初始化时使用 `HOME=/root` 并加载 `/root/.bashrc`，使其中的 NVM、PATH 等设置生效；执行实际命令前恢复独立 `HOME=/home/node`，会话和运行时配置继续按需求隔离。镜像中的 `/root` 不会被挂载遮住。root 模式下初始化文件缺失或不可读会明确失败，需修正镜像或运行配置后重试。

root 模式使用 `0:<宿主 GID>`，增加读取独立 HOME 私有文件所需的 `DAC_OVERRIDE` 能力；只读源码挂载仍保持只读。
Git 仅信任当前导出的工作区和已选子仓库、全景上下文仓库，避免宿主属主与 root 不同导致 `dubious ownership`；不会信任未选目录或使用全局通配信任。

服务启动、Agent 任务、构建/验证、预检中的 Node、版本与命令入口检查、应用启动及 HTTP 检查、全景只读检查均采用这一机制。每次业务执行都会初始化，保留原 stdin、工作目录、参数、退出码和信号；初始化输出不混入业务 stdout/stderr。受控 PostgreSQL 的查询、备份和恢复不加载业务初始化文件。

更改 `run_as_root` 或 Bash 初始化内容后，停止已有环境并同步最新配置，重新预检通过后再启动。

控制服务仍需支持约定的 OpenCode 兼容 HTTP、认证和会话接口。Node、Git、固定 Superpowers、镜像一致性、只读挂载与项目命令入口继续检查；预检不调用模型，也不代表业务构建或测试完成。

## 查看失败原因

在需求详情展开“环境与手动工具”→“项目环境与预检”，查看本次检查的 CLI 名称、服务命令、版本命令与实际版本。旧报告的 `opencode_version` 继续兼容显示，新报告使用中性的 `agent_cli_service` 与 `agent_cli_version`。

- Agent 服务已退出：及时失败，显示所选 CLI、实际命令、独立退出码与启动日志，无需等满 90 秒；code-server 等默认服务可以仍在运行。
- 容器默认服务已退出：单独显示容器状态、退出码与容器启动日志。检查 Dockerfile 的原有入口是否能在当前用户、HOME 与挂载设置下持续运行。
- 控制接口尚未就绪：最多等待 90 秒，之后显示实际 CLI 和启动诊断。检查封装的监听参数、必要配置和兼容接口。
- 版本检查失败：检查配置中的版本程序和参数，确认镜像包含对应程序；封装无需叫 `opencode`。
- 初始化失败：检查镜像是否有 `/bin/bash`；root 模式还需存在可读的 `/root/.bashrc`。CLI 安装在 `/root` 时确认已启用 root 模式，并将新配置同步到当前需求。
- 程序未能启动：`executable file not found in $PATH` 或初始化后的“找不到命令”表示尚未进入 CLI 服务检查。预检会保存配置中的实际程序、PATH、HOME、容器用户与启动错误；Docker inspect 的入口是镜像默认服务，不能据此判断实际 CLI 配置。诊断读取失败时保留原始启动错误。
- 启动后恢复正常：通过的报告不会继续显示启动初期的连接失败。

Agent 服务的状态、退出码和日志独立记录，不依赖 `docker logs` 判断 exec 进程是否仍在运行；容器日志用于诊断默认服务。启动日志按实际配置的凭据值脱敏，只读取本次预检记录的受管容器。预检结束会停止该容器；清理无法核实时仍按原规则显示 UNKNOWN，先停止并核实再继续。

### CLI 要求登录时，强制启动环境排障

如果 CLI 要求登录，导致预检无法通过，可在需求详情的“环境与手动工具”或“容器管理”点击“强制启动环境（排障）”。先创建工作区；已有运行记录时先停止，遇到 UNKNOWN 时使用“停止并核实”。需要检查新修改的项目配置时，先停止并“同步最新配置”。

此操作跳过预检门禁，只启动容器，不执行工作台配置的 CLI 服务命令，也不等待控制接口就绪。容器沿用当前需求固定的镜像、运行用户、独立 `HOME`、环境变量、配置及目录挂载、资源限制和端口配置；配置了开发数据库时也会按原规则启动。镜像自己的 `ENTRYPOINT` 和 `CMD` 保留，因此默认服务仍须能持续运行；若镜像默认入口本身就启动 CLI 并要求登录，需要先调整镜像入口。

启动成功后显示“排障容器运行中”，页面提供容器名、进入容器命令及固定 CLI 服务命令。使用页面的进入容器命令，在同一用户和 `HOME=/home/node` 下检查凭据文件路径、权限及 CLI 登录状态。只读配置文件挂载来自准备或同步配置时的副本；目录挂载按项目配置使用原目录。不要将凭据值粘贴到工作台日志或问题报告中。

root 模式的工作台命令会先以 `HOME=/root` 加载 `/root/.bashrc`，再恢复 `HOME=/home/node`。手动复现时也需保留这一行为；直接切换到 root 登录 Shell 会改变 HOME，可能读到另一套登录凭据。只检查路径和可读性时可使用 `id`、`printf '%s\n' "$HOME"` 和 `test -r <凭据文件路径>`，无需输出文件内容。手动执行 CLI 登录前，确认 CLI 实际使用的配置路径，以及该路径是否为只读挂载。

排障启动不会把预检改成通过，也不代表 Agent 已就绪。排障期间不能执行工作台的开发、构建、验证或应用启动任务。检查完成后停止环境；若修改了宿主配置文件，保存并同步最新配置，再重新执行“环境预检”，通过后按正常方式“启动环境”。代码和独立 Agent HOME 在停止后保留；预检使用独立只读副本和 Agent HOME，开发容器中的手动登录不会自动同步到预检副本。

可先从本机进入镜像内的 root Bash，不需要发送密钥：

```powershell
docker run --rm -it --user 0 --env HOME=/root --entrypoint=/bin/bash your-wrapper-image:tag
```

随后在容器中执行：

```bash
source /root/.bashrc
team-code --version
```

### 镜像找不到 CLI 时如何处理

界面填写的是 **Linux 容器内**的程序路径。宿主 Windows 能运行同名程序，不代表镜像已安装它。若程序在镜像 PATH 外，例如 `/opt/team/bin/team-code`，请为 CLI 新版本的三条命令都使用该路径：

```text
/opt/team/bin/team-code serve --hostname {host} --port {port}
/opt/team/bin/team-code run --attach {endpoint} --model {model} --format json
/opt/team/bin/team-code --version
```

或在 Dockerfile 中将程序安装到公共目录并设置镜像 PATH，例如：

```dockerfile
COPY --chmod=755 team-code /opt/team/bin/team-code
ENV PATH="/opt/team/bin:${PATH}"
```

程序及所需解释器必须对选定容器用户可执行。CLI 位于 `/root` 或由 root 的 NVM 管理时使用上述 root 模式；普通用户模式可将程序安装到 `/opt` 或 `/usr/local/bin` 等可读目录。工作台为每个需求设置独立 `HOME=/home/node` 并挂载到该目录，镜像中仅安装到 `/home/node/.local/bin` 等目录的程序会被挂载遮住。将可写配置与程序分开。配置命令中的 `~`、`$HOME`、Shell 别名和宿主路径不会自动展开；Bash 初始化会设置执行环境，但命令及参数仍按原值传递。

修改 CLI、镜像或运行用户后，保存新的配置版本，在项目运行配置中选择它；已有需求停止环境后同步最新配置，或准备新需求工作区，再执行预检。修改默认配置不会自动替换已有需求的固定 CLI 版本和镜像 ID。单纯更新工作台代码后，可以在原需求重跑以查看新增诊断。

## 验证边界

强制启动排障的 Docker/API/Runner 验证：

```bash
.venv/bin/python poc/verify_force_start.py --root /tmp/awb-force-start-new
```

该脚本构建一个明确要求登录并退出的受控 CLI，验证预检失败后仍可强制启动、保留固定配置与只读凭据挂载、手动复现登录错误及停止清理；不使用真实用户凭据或调用模型。结果见[排障启动验收记录](evidence/force-start-report.json)。需要 Docker 与 server 依赖，`--root` 必须是新目录；结束时清理本次容器和镜像。

不依赖浏览器的动态服务验证：

```bash
.venv/bin/python poc/verify_agent_service.py --root /tmp/awb-agent-service-new
```

该脚本用同一个镜像保留默认 HTTP 服务，分别按配置启动两个 Agent CLI，并验证独立退出码、缺失程序、停止清理和源码保留；不调用模型。默认 HTTP 服务是受控 fixture，不是真实 code-server，结果见[动态服务验收记录](evidence/agent-service-report.json)。

`poc/verify_preflight_cli.py` 使用本地测试镜像，PATH 中不提供 `opencode`，只提供 `team-code`、Node、Git 和 Shell。镜像的默认入口运行一个常驻的非 Agent fixture，验证原有入口和命令保留、CLI 通过 exec 启动，以及默认服务仍在运行时 CLI 退出能及时报错。通过真实 API、Runner、Docker 和浏览器检查预检、正常启动、缺失程序诊断，以及 PATH 外的绝对路径 CLI。

该服务是受控的兼容 HTTP fixture，不执行模型推理，不代表用户私有封装镜像已现场验证。结果见[预检验收记录](evidence/preflight-wrapper-report.json)。

```bash
.venv/bin/python poc/verify_preflight_cli.py --root /tmp/awb-preflight-cli-new
```

需要已有 `agent-workbench-m1:local` 基础镜像、Docker、Playwright 和 Chromium。`--root` 必须是新的独立目录；脚本清理本次容器和镜像标签，并保留私有日志、数据与截图。
