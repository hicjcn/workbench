# M1 技术验证操作说明

- 日期：2026-10-24
- 目标：验证独立源码、Docker 环境、OpenCode 控制 API、Superpowers 加载和重建持久化
- 验证模型：按用户最新要求切换为 `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`，使用用户现有 OpenRouter 密钥
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

## 7. 免费模型真实验证

### 当前：OpenRouter 与用户现有密钥

默认模型已切换为 `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`。运行前，将用户现有密钥通过云环境 Secret 注入为 `OPENROUTER_API_KEY`，并将 `openrouter.ai` 加入出站白名单后应用到当前运行实例。只检查变量是否存在和就绪，不打印变量值，不把密钥粘贴进聊天、命令参数或项目文件。无需创建新密钥。

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-05 \
  --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free \
  --proxy-ca "$CODEX_PROXY_CERT" --timeout 600
```

普通本机不需要代理 CA 时省略 `--proxy-ca`。每次使用新根目录。脚本先检查已有密钥，再通过无认证的公开 OpenRouter 目录核实指定 `:free` 模型存在、prompt/completion 价格完整且所有返回的价格项为有限数值 0；失败时不提交模型任务。目录可达不等于真实推理或代码修改成功。

密钥只通过 Docker 的环境变量名传入服务，不写入命令参数值、生成的 OpenCode 配置、运行状态或验证报告。配置仅启用 OpenRouter，模型白名单、默认模型及辅助模型均固定为同一个指定 `:free` 模型；重新启动时也会重设这些限制，不配置其他模型回退。验证输出、错误、测试结果和代码差异中的已知密钥值会被替换为 `[REDACTED]`；超时的部分输出同样处理。Agent 主目录及完整 Docker/OpenCode 配置不能作为公开证据提交。

真实任务仍按下述相同验收条件判断。报告增加 `provider`、公开目录零价格检查结果、`credentials_injected` 与密钥变量名，绝不记录密钥值。使用用户现有密钥与免费模型，并不代表免费配额、速率或可用性一定满足本次任务。

### 历史：OpenCode 免凭据免费模型

用户已明确使用免凭据模型，并在 GLM、MiMo 免费额度阻塞后要求改试 DeepSeek V4.1 Flash Free。容器实际模型列表及 [OpenCode 官方模型目录](https://opencode.ai/zen/v1/models) 均包含 `deepseek-v4.1-flash-free`。通过 `--model` 明确选择本轮模型，不注入模型凭据，不配置付费回退。

网络可访问且服务端免费额度可用后执行（run-01 至 run-04 已有历史证据，后续使用新目录）：

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-05 \
  --model opencode/deepseek-v4.1-flash-free
```

托管代理环境额外传入 `--proxy-ca "$CODEX_PROXY_CERT"`。脚本创建新的无敏感信息测试仓库，发起一次真实修改任务，再独立检查模型响应、实现文件变化、回归测试文件变化、至少 4 项测试及验收行为，保存事件及差异，最后停止容器。仅列出模型、CLI 返回成功或测试未失败都不足以判定通过。

证据目录包括 `approved-task.txt`、`before-snapshot.json`、`after-snapshot.json`、`agent-events.jsonl`、`agent-stderr.txt`、`test-output.txt`、`acceptance-output.txt`、`code-diff.patch` 和带起止时间的 `report.json`。中途失败时可能只有部分文件；超时会保留已捕获的事件和标准错误，不将部分输出判为成功。Agent 主目录及控制状态不可直接提交为公开证据。

若执行超时，记录未知状态并停止容器，不自动重复提交。默认每次验证最多等待 240 秒，选择新根目录才能重新运行。

run-01 被代理 tunnel 403 阻塞；run-02 服务可达但 GLM 免费额度耗尽。run-03 改用 MiMo、最新 run-04 改用 DeepSeek，均已确认实际会话模型，但同样收到服务端 `free_tier_limit`，详细证据见验证报告。仅用默认 Python 客户端探测可能收到 `error code: 1010` 的 HTTP 403，需要结合实际 OpenCode 会话区分代理拒绝、客户端差异及模型额度问题。始终保留继承代理及 TLS 校验。

CLI 等待期间可通过本地控制 API 的 `/session/status` 观察额度状态；本轮额度提示没有进入 CLI JSON 事件。保存该状态后，只中止此次验证会话并让脚本完成验收与清理。CLI 返回 0 且没有事件仍必须按模型响应、代码变化和独立行为验收判定；不能据此宣布模型任务通过。服务端给出的下次重试时间不保证届时额度恢复，后续验证应显式选择新根目录，不自动重放、不创建模型凭据、不切换付费模型。

### 其他用户自有模型配置

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

