# M1 技术验证操作说明

- 日期：2026-10-24
- 目标：验证独立源码、Docker 环境、OpenCode 控制 API、Superpowers 加载和重建持久化
- 首轮模型：按用户最新要求使用免凭据免费模型，当前候选 `opencode/glm-5.3-flash-free`
- 平台：当前实现面向 Linux、Unix Docker Socket、Python 3.11+、Git；测试项目使用 Node.js

## 1. 当前可运行范围

仓库新增一个无第三方 Python 运行依赖的命令行原型，提供：

- 导出指定 Git 提交，建立需求独立代码仓库。
- 可选保留当前未提交及选定未跟踪源码。
- 计算覆盖工作区内容的快照摘要，识别未提交变化。
- 创建受限权限的 OpenCode 容器，探测控制 API 和技能。
- 停止运行资源并保留代码、Agent 配置及数据。
- 使用两个真实容器执行可复现的隔离和重建验证。

M1 尚不包含 Web 页面、完整流程引擎、评审调度或生产交付。设计示例目录仍不是可执行配置；此原型通过 CLI 参数运行。

## 2. 固定工具版本

| 工具 | 本次选择 | 来源 |
| --- | --- | --- |
| OpenCode | 1.19.4 | 2026-10-24 从 npm `opencode-ai` 读取的当前版本 |
| Superpowers | `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`，v7.0.1 | 官方仓库当时的 HEAD |
| 基础镜像 | node:22-bookworm-slim | 实际运行镜像摘要记录在验证报告 |

用户要求使用最新版；验证时将解析出的具体版本固定在 Dockerfile，避免后续自动更新改变结果。安装方式参考 [Superpowers 的 OpenCode 安装说明](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/.opencode/INSTALL.md)。

## 3. 运行基础检查

以下命令在仓库根目录执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --test poc/sample-project/server.test.mjs
PYTHONPATH=src python3 -m agent_workbench doctor
```

本地控制接口测试需要允许绑定回环地址。CI 运行基础测试与测试项目检查，真实 Docker 验证单独执行，不能以 CI 基础测试通过代替容器验证。

## 4. 构建验证镜像

普通本机环境：

```bash
docker build -t agent-workbench-m1:local poc/container
```

需要会话代理证书的托管环境：

```bash
docker build \
  --secret "id=proxy_ca,src=$CODEX_PROXY_CERT" \
  -t agent-workbench-m1:local poc/container
```

构建使用已有 Docker 代理配置。证书通过 BuildKit secret 临时挂载，不写入镜像；不关闭 TLS 验证。Docker 守护进程不可访问时先处理环境权限或状态，不通过特权 Agent 容器绕过。

## 5. 一次性双容器验证

```bash
PYTHONPATH=src python3 poc/verify_isolation.py \
  --root .local-data/m1-run-01
```

托管代理环境额外传入 `--proxy-ca "$CODEX_PROXY_CERT"`。每次选择新的验证根目录，程序不会覆盖已有证据。

验证内容：

1. 将内置测试项目初始化为源仓库，导出为两个需求副本。
2. 创建两套独立 Docker 网络、Agent 主目录及 OpenCode 控制端口。
3. 检查健康状态、OpenAPI、技能发现，以及空会话的创建/中止/删除。
4. 在需求 A 的容器内改动代码，核实需求 B 和源仓库不受影响。
5. 在两个容器内分别执行测试项目测试。
6. 删除并重建需求 A 的运行容器，核实代码和 Agent 主目录保留，需求 B 仍然健康。
7. 停止此次创建的容器与可移除网络，保留源码和证据。

报告保存为所选目录下的 `report.json`；该目录已被 Git 忽略。报告中的 `PASSED_INFRASTRUCTURE_ONLY` 仅说明基础设施验证成功，不能解释为真实模型开发验证或完整 M1 已完成。

## 6. 使用自己的试点项目

```bash
PYTHONPATH=src python3 -m agent_workbench create \
  --source /absolute/path/to/backend-repository \
  --root /absolute/path/to/workbench-data \
  --requirement req-a --project backend

PYTHONPATH=src python3 -m agent_workbench start \
  --workspace /absolute/path/to/workbench-data/req-a/backend

PYTHONPATH=src python3 -m agent_workbench probe \
  --workspace /absolute/path/to/workbench-data/req-a/backend

PYTHONPATH=src python3 -m agent_workbench snapshot \
  --workspace /absolute/path/to/workbench-data/req-a/backend

PYTHONPATH=src python3 -m agent_workbench stop \
  --workspace /absolute/path/to/workbench-data/req-a/backend
```

默认仅导出 HEAD 提交。需要本地改动时，在 create 中使用 `--include-dirty`，需要未跟踪文件时再加 `--include-untracked`。源码与工作区根目录不能包含彼此。

当前镜像仅包含 Node、Git 和 Agent 工具。Java、Python、数据库等真实业务项目运行环境需要替换或扩展 Dockerfile；不能直接宣称与用户本机一致。

## 7. 免凭据真实模型验证

用户已明确将首轮验证改为免凭据模型。容器的实际模型列表包含 `opencode/glm-5.3-flash-free`，与 [OpenCode 官方免费模型目录](https://opencode.ai/v2/docs/console/models/) 对应。本次不注入模型凭据，不配置付费回退。

网络可访问模型服务后执行：

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-01 \
  --model opencode/glm-5.3-flash-free
```

托管代理环境额外传入 `--proxy-ca "$CODEX_PROXY_CERT"`。脚本创建新的无敏感信息测试仓库，发起一次真实修改任务，再独立检查代码变化、回归测试文件变化、至少 4 项测试及验收行为，保存事件及差异，最后停止容器。仅列出模型、CLI 返回成功或测试未失败都不足以判定通过。

若执行超时，记录未知状态并停止容器，不自动重复提交。默认每次验证最多等待 240 秒，选择新根目录才能重新运行。

当前托管环境代理对 `opencode.ai` 返回 tunnel 403，模型实际调用等待网络配置更新。需要允许 `opencode.ai`，建议同时允许模型目录 `models.dev`。这与是否提供模型凭据无关。

### 后续使用自己的模型

本机验证可在已经启动的目标容器内，通过 OpenCode 自身的交互登录配置现有智谱认证：

```bash
docker exec -it <container-name> opencode auth login
docker exec -it <container-name> opencode attach http://127.0.0.1:4096
```

`<container-name>` 来自 start 命令输出。attach 连接现有服务，认证由容器已有环境提供，不需要把控制密码放入命令行。智谱标准 API 和 Coding Plan 的 provider 可能不同，按现有服务与实际模型列表选择，不能仅根据模型简称猜测。

凭据只输入 OpenCode 的本地交互界面，或者通过受控运行环境注入；不要发送到聊天或放入项目源码。Agent 主目录按需求隔离并保留在工作区中，包含认证信息时也不能提交 Git。

建议真实模型任务：给测试项目的 greeting 增加空白处理，纯空白名称返回默认问候，并补充测试。依次记录执行前快照、Agent 会话、执行后快照、测试输出及代码差异；再验证真实任务中止和重新接入。

用户自有模型接入仍可作为后续配置，运行环境需要具备对应服务地址的网络访问权限。

## 8. 原型边界和已知限制

- 源码采用目标提交快照导出，并建立新的 Git 历史；保存原始提交 SHA，但不是完整历史克隆，尚不能直接推送为源仓库同源分支。
- 导出时过滤 `.env`、凭据目录及常见生成目录；过滤不等同于识别所有形式的秘密，试点项目仍需明确配置范围。
- 为避免历史凭据进入 Agent，不导入原仓库 `.git`；后续历史导入需要单独设计。
- 当前拒绝子模块、LFS 指针和符号链接，避免静默生成不完整或越界代码。
- 100 MiB 是当前源码导出限制，不是运行时磁盘硬配额。
- 容器源码可写、根文件系统只读，无特权模式和 Docker Socket；没有实现严格域名出站限制。
- 当前没有数据库/缓存隔离验收，内置项目也不依赖这些服务。
- Runtime 使用本地文件日志和互斥；数据库租约、未知执行核实及完整步骤恢复属于后续实现。
- 空会话中止通过，不代表真实模型任务的中止、断线恢复和事件完整性已经通过。
- 当前自动测试用显式异常或断言表达验收条件，运行验证脚本时不能使用 Python `-O`。

实测结果及后续待办见 [M1 验证报告](05-m1-validation.md)。

