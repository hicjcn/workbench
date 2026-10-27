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

需求详情显示封装名称，说明已选择该配置。预检在独立只读副本继承同一版本；后续修改默认值或创建 CLI 配置新版，不会静默替换已准备需求的绑定。准备前在项目运行配置选择对应 CLI，或设为新工作区默认。

运行时使用 `--entrypoint=<服务命令中的程序>` 显式覆盖镜像默认入口，其余参数单独传递，不依赖空字符串参数。这也避免 Windows 上经过命令转发后空参数丢失。例如镜像有 `ENTRYPOINT ["team-code"]` 时，仍只运行一次 `team-code serve ...`。如果镜像原入口包含必要初始化，请将入口脚本写进完整服务命令，例如：

```text
/usr/local/bin/agent-entry.sh team-code serve --hostname {host} --port {port}
```

控制服务仍需支持约定的 OpenCode 兼容 HTTP、认证和会话接口。Node、Git、固定 Superpowers、镜像一致性、只读挂载与项目命令入口继续检查；预检不调用模型，也不代表业务构建或测试完成。

## 查看失败原因

在需求详情展开“环境与手动工具”→“项目环境与预检”，查看本次检查的 CLI 名称、服务命令、版本命令与实际版本。旧报告的 `opencode_version` 继续兼容显示，新报告使用中性的 `agent_cli_service` 与 `agent_cli_version`。

- 服务容器已退出：及时失败，显示所选 CLI、实际命令、退出码与启动日志，无需等满 90 秒。
- 控制接口尚未就绪：最多等待 90 秒，之后显示实际 CLI 和启动诊断。检查封装的监听参数、必要配置和兼容接口。
- 版本检查失败：检查配置中的版本程序和参数，确认镜像包含对应程序；封装无需叫 `opencode`。
- 程序未能启动：`executable file not found in $PATH` 表示容器找不到入口程序，尚未进入 CLI 服务检查。预检会保存实际程序、PATH、HOME、容器用户和 Docker 错误，即使控制服务完全没有启动也能查看。诊断读取失败时保留原始启动错误。
- 启动后恢复正常：通过的报告不会继续显示启动初期的连接失败。

启动日志按实际配置的凭据值脱敏，只读取本次预检记录的受管容器。预检结束会停止该容器；清理无法核实时仍按原规则显示 UNKNOWN，先停止并核实再继续。

可在本机先验证程序名，不需要发送密钥：

```powershell
docker run --rm --entrypoint=team-code your-wrapper-image:tag --version
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

程序及所需解释器必须对工作台容器用户可执行。工作台为每个需求设置独立 `HOME=/home/node` 并挂载到该目录；构建时只安装到 `/home/node/.local/bin` 等目录的程序会被挂载遮住。建议安装到 `/opt` 或 `/usr/local/bin`，并将可写配置与程序分开。`~`、`$HOME`、Shell 别名和宿主路径不会自动展开；若原镜像入口负责设置 PATH，需将必要初始化加入显式包装脚本，并保证服务、任务及版本检查均可执行。

修改 CLI 或镜像后，保存新的配置版本，在项目运行配置中选择它并准备新需求工作区，再执行预检。已有需求仍使用原固定 CLI 版本和镜像 ID，修改默认配置不会替换它们。单纯更新工作台代码后，可以在原需求重跑以查看新增诊断。

## 验证边界

`poc/verify_preflight_cli.py` 使用本地测试镜像，PATH 中不提供 `opencode`，只提供 `team-code`、Node、Git 和 Shell，并设置 CLI 默认 ENTRYPOINT。通过真实 API、Runner、Docker 和浏览器验证预检、正常启动、服务退出、启动前缺失程序的诊断，以及 PATH 外的绝对路径 CLI。

该服务是受控的兼容 HTTP fixture，不执行模型推理，不代表用户私有封装镜像已现场验证。结果见[预检验收记录](evidence/preflight-wrapper-report.json)。

```bash
.venv/bin/python poc/verify_preflight_cli.py --root /tmp/awb-preflight-cli-new
```

需要已有 `agent-workbench-m1:local` 基础镜像、Docker、Playwright 和 Chromium。`--root` 必须是新的独立目录；脚本清理本次容器和镜像标签，并保留私有日志、数据与截图。
