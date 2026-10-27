# Windows 11 兼容性与启动

Windows 上采用 **原生 Python API/Runner + Docker Desktop 的 Linux 容器**。页面仍由本机 API 提供，配置和记录存入本地 SQLite，无需安装数据库服务。也可以把整套控制进程放到 WSL2，按 README 中的 Linux 命令启动。

**推荐使用新增的[统一配置与启停脚本](28-local-configuration.md)**：复制 `workbench.example.toml` 为 `workbench.toml`，运行 `setup.cmd`，之后用 `start.cmd`、`status.cmd` 和 `stop.cmd` 管理服务。本文后续命令用于手动运行和兼容性排查。

## 兼容性检查与修复

此前版本直接导入 `fcntl`，无法在 Windows 原生 Python 中加载 API 和 Runner。仅去掉这一个导入还不足以运行，以下依赖也一并调整：

| 差异 | 处理方式 |
| --- | --- |
| `fcntl.flock` 不存在 | 统一文件锁接口。Linux 保持 `flock`；Windows 使用 portalocker 的 Win32 `LockFileEx`，保留共享、排他、非阻塞及关闭释放语义 |
| API/Runner 共用数据目录 | 两者持共享控制锁；离线维护要求排他锁。Windows 也不能在运行中备份或恢复同一目录 |
| Docker socket 固定为 Unix 路径 | Windows 固定连接本机 `npipe:////./pipe/dockerDesktopLinuxEngine`，Linux 保持本机 Unix socket；仍忽略可重定向的 Docker host/context 环境变量 |
| `os.getuid/getgid` 不存在 | Windows Linux 容器使用 `1000:1000`，不映射 Windows SID；Linux 保持宿主 UID/GID 行为 |
| `pass_fds` 只支持 POSIX | Windows 镜像维护通过独立守护子进程持操作锁，握手完成后才执行 Docker；原 Runner 结束不释放仍在执行的命令锁 |
| Docker Desktop 挂载路径转换 | 构建挂载参数时保留盘符、空格和中文；核对挂载时只接受原 Windows 路径或已知的 Desktop `/run/desktop/mnt/host/<盘符>/...`、`/host_mnt/<盘符>/...` 映射 |
| 默认文本编码不同 | 私有 JSON/配置文件显式使用 UTF-8，Docker 任务的输入输出按 UTF-8 处理 |
| NTFS 没有 POSIX 执行位 | Windows 快照根据 Git index 保存执行标记，导出工作区时保留已提交脚本的执行标记；工作区关闭自动换行转换 |
| Windows 特有路径 | 拒绝盘符逃逸、反斜杠相对路径、NTFS alternate data streams、设备名、大小写冲突及尾随点/空格；宿主路径上的重解析点、junction 和运行时新增链接仍拒绝。Git 已提交且指向本仓库内的相对链接，在导出时展开为普通文件/目录，无需 Windows 创建符号链接权限 |

Windows 专用依赖由 `pip install -e '.[server]'` 自动安装，不需要也不应单独安装 `fcntl`。

## 1. 准备环境

- Windows 11，建议原生 Python 3.12（最低 3.11）、Node.js 22.12+、Git for Windows。
- 安装并启动 Docker Desktop，启用 **Use the WSL 2 based engine**，选择 **Linux containers**。首次启用 WSL2/虚拟化后可能需要重启 Windows。
- 源码和数据使用当前用户可读写的本地 NTFS 目录，建议路径简短，例如 `C:\dev\workbench`。长路径项目还需启用 Windows/Git 对长路径的支持。
- 当前不支持把原生 Runner 的源码或数据放在 UNC/SMB 网络共享、WSL UNC 路径或 OneDrive 按需下载目录；这些目录的文件锁、重解析点与挂载语义不同。
- 镜像内仍运行 Linux 工具。表单中的 CLI、构建/验证命令使用容器路径和 Linux 命令，不填 `.exe`、PowerShell 命令或宿主 Python 路径。

在 PowerShell 中检查：

```powershell
py -3 --version
node --version
npm.cmd --version
git --version
docker info --format '{{.OSType}}'
docker --host=npipe:////./pipe/dockerDesktopLinuxEngine info --format '{{.OSType}}'
```

最后两条都应输出 `linux`。若无法连接，先启动 Docker Desktop 并检查 Linux engine；启用 Windows containers 不能运行本项目镜像。使用 `npm.cmd` 可避免 PowerShell 将 `npm` 解析成受执行策略限制的 `npm.ps1`，无需修改全局执行策略。

## 2. 获取、安装和构建

以下命令在 PowerShell 执行，不需要激活虚拟环境。已有仓库时，进入仓库根目录后从 venv 安装步骤开始。

```powershell
git clone https://github.com/hicjcn/workbench.git
Set-Location workbench

$env:PYTHONUTF8 = "1"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[server]'
npm.cmd ci --prefix web
npm.cmd run --prefix web build
docker build -t agent-workbench-m1:local poc/container
```

代理/自定义 CA 环境仍需要为 Docker 构建和容器配置正确的代理与可信 CA；不要关闭 TLS 校验。自定义 CLI 也必须安装在项目镜像内，不能只安装在 Windows 宿主机。

## 3. 终端一：API 与页面

在仓库根目录启动：

```powershell
$env:PYTHONUTF8 = "1"
$env:AWB_MODEL = "opencode/glm-5.3-flash-free"
.\.venv\Scripts\python.exe -m agent_workbench.server --sqlite --data-dir "$PWD\.local-data\workbench"
```

启动自动创建/升级 SQLite。`--sqlite` 明确使用该目录中的 `workbench.db`，忽略遗留的外部数据库 URL。

这里的免费模型只是支持的配置示例，其当前是否开放、额度是否可用仍需核实。当前不支持直接填写任意厂商模型或付费回退，详见 README 的模型配置。

## 4. 终端二：Runner

另开 PowerShell，进入**同一个仓库根目录**后执行：

```powershell
$env:PYTHONUTF8 = "1"
$env:AWB_MODEL = "opencode/glm-5.3-flash-free"
.\.venv\Scripts\python.exe -m agent_workbench.runner --data-dir "$PWD\.local-data\workbench" --api http://127.0.0.1:8340
```

两个终端需要相同绝对数据目录和模型配置；可在同一个 `workbench.toml` 中设置，手动入口也支持 `--config`。模型凭据通过该私有文件的 `[environment]` 或既有进程环境提供，不放入聊天、仓库或 Dockerfile。工作台不自动加载其他 `.env` 文件。

## 5. 登录与接入项目

打开 <http://127.0.0.1:8340>，在本机读取 `.local-data\workbench\access-token`，填入登录表单。不要把令牌提交 Git 或分享出去。

1. 在「资源与恢复」检查 Runner、Docker 和 SQLite 状态。
2. 在「项目登记」填写现有 Git 仓库的 Windows 绝对路径，例如 `C:\dev\order-service`；仓库需要已有提交，且与工作台数据目录互不包含。
3. 保存项目镜像、构建/验证命令和 Agent CLI 配置。Dockerfile 中的路径、只读配置的**容器目标路径**仍为 `/run/workbench-config/...`；只有宿主源路径使用 Windows 路径。
4. 新建需求并创建工作区，运行环境预检，再启动环境、构建和验证。预检不调用模型，不能代替实际模型或业务测试。

可先接入自带 Node 样例，在 PowerShell 中创建独立 Git 源码仓库：

```powershell
docker build -t agent-workbench-node:local poc/onboarding
$sampleProjectDir = Join-Path $env:USERPROFILE 'workbench-samples\node-service'
if (Test-Path $sampleProjectDir) { throw '样例目录已存在，请选择新目录' }
New-Item -ItemType Directory -Path (Split-Path $sampleProjectDir) -Force | Out-Null
Copy-Item -Recurse poc/sample-project $sampleProjectDir
git -C $sampleProjectDir init -b main
git -C $sampleProjectDir add --all
git -C $sampleProjectDir -c user.name='Workbench Sample' -c user.email='sample@example.invalid' commit -m 'Initial sample'
```

登记此目录，使用镜像 `agent-workbench-node:local`、构建命令 `node --check server.mjs`、验证命令 `node --test server.test.mjs`，无需启用业务数据库。后续流程见 README。

## 6. 停止与离线备份

先在页面停止需求环境、核实活动/未知任务，再用 `Ctrl+C` 分别结束 API 和 Runner。结束 Python 进程不会自动停止容器。

在仓库根目录离线执行：

```powershell
.\.venv\Scripts\python.exe -m agent_workbench.maintenance stop --sqlite --data-dir "$PWD\.local-data\workbench"
.\.venv\Scripts\python.exe -m agent_workbench.maintenance backup --sqlite --data-dir "$PWD\.local-data\workbench"
```

恢复示例，ZIP 替换为实际路径，目标目录必须尚不存在：

```powershell
.\.venv\Scripts\python.exe -m agent_workbench.maintenance restore --archive 'C:\private\backup.zip' --destination "$PWD\.local-data\workbench-restored"
```

恢复后改用新数据目录和新令牌。镜像、模型凭据及只读配置文件仍需单独准备。**Linux 与 Windows 数据目录/离线备份的跨系统迁移尚未作为支持能力验收**，不要直接复制旧工作区接着运行；应在目标系统准备新的数据目录并重新登记项目。

Windows 的 `chmod(0600)` 不等于设置 NTFS 私有 ACL。请把整个数据目录放在当前用户专用目录，并使用 Windows 文件夹“安全”设置限制其他用户访问；工作台不自动重写系统 ACL。

## 验证与边界

CI 增加 `windows-latest` 与 `ubuntu-latest` 双平台检查，运行同一套 Python/API 测试、Node 样例测试和前端构建。专项测试实际启动 API 与 Runner 进程，在 SQLite 中登记项目并准备独立工作区，同时检查跨进程共享/排他锁、守护进程、Git 快照和 UTF-8 记录；Windows 还检查 NTFS junction 拒绝，并解析本指南的 PowerShell 代码块以验证语法。

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test poc/sample-project/server.test.mjs
npm.cmd run --prefix web build
```

CI 中的 Docker 命令使用明确的测试替身，不连接模型。当前开发环境为 Linux，无法在本机进行 Windows 11 + Docker Desktop 的真实容器验收；Docker Desktop 挂载、文件权限、网络与实际 CLI 任务仍需在目标 Windows 主机完成验证。`poc/verify_*.py` 中使用系统 Chromium/Linux 工具的历史验收脚本仍面向 Linux，不保证可原样在 PowerShell 执行。

如果希望使用已有 Linux 验收脚本，可在 WSL2 Ubuntu 内安装 Python、Node、Git 并开启 Docker Desktop 的 WSL integration，把源码及数据放在 WSL 的 `/home/...` 下，API 与 Runner 全部从同一个 WSL 环境启动，再通过 Windows 浏览器访问本机地址。不要混用 Windows 原生与 WSL 进程管理同一个数据目录。
