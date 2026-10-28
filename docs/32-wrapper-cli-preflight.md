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

运行时使用 `/bin/bash` 包装器覆盖镜像默认入口，先加载初始化文件，再执行配置中的实际程序。原命令及参数独立传递，不把它们拼接成 Shell 字符串。预检报告仍显示真实的 CLI 服务、任务和版本命令；Docker inspect 中的入口则会显示 Bash。镜像自带的其他 ENTRYPOINT 初始化不会自动执行，必要时将入口脚本写进完整服务命令，例如：

```text
/usr/local/bin/agent-entry.sh team-code serve --hostname {host} --port {port}
```

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

- 服务容器已退出：及时失败，显示所选 CLI、实际命令、退出码与启动日志，无需等满 90 秒。
- 控制接口尚未就绪：最多等待 90 秒，之后显示实际 CLI 和启动诊断。检查封装的监听参数、必要配置和兼容接口。
- 版本检查失败：检查配置中的版本程序和参数，确认镜像包含对应程序；封装无需叫 `opencode`。
- 初始化失败：检查镜像是否有 `/bin/bash`；root 模式还需存在可读的 `/root/.bashrc`。CLI 安装在 `/root` 时确认已启用 root 模式，并将新配置同步到当前需求。
- 程序未能启动：`executable file not found in $PATH` 或初始化后的“找不到命令”表示尚未进入 CLI 服务检查。预检会保存配置中的实际程序、PATH、HOME、容器用户与启动错误；Docker inspect 的入口可能是 Bash，不能据此认为实际 CLI 配置已更换。诊断读取失败时保留原始启动错误。
- 启动后恢复正常：通过的报告不会继续显示启动初期的连接失败。

启动日志按实际配置的凭据值脱敏，只读取本次预检记录的受管容器。预检结束会停止该容器；清理无法核实时仍按原规则显示 UNKNOWN，先停止并核实再继续。

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

`poc/verify_preflight_cli.py` 使用本地测试镜像，PATH 中不提供 `opencode`，只提供 `team-code`、Node、Git 和 Shell，并设置 CLI 默认 ENTRYPOINT。通过真实 API、Runner、Docker 和浏览器验证预检、正常启动、服务退出、启动前缺失程序的诊断，以及 PATH 外的绝对路径 CLI。

该服务是受控的兼容 HTTP fixture，不执行模型推理，不代表用户私有封装镜像已现场验证。结果见[预检验收记录](evidence/preflight-wrapper-report.json)。

```bash
.venv/bin/python poc/verify_preflight_cli.py --root /tmp/awb-preflight-cli-new
```

需要已有 `agent-workbench-m1:local` 基础镜像、Docker、Playwright 和 Chromium。`--root` 必须是新的独立目录；脚本清理本次容器和镜像标签，并保留私有日志、数据与截图。
