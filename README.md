# Agent 开发工作台

面向后端开发者的本地 Agent 开发工作台。以 OpenCode 为主要执行工具，结合 Superpowers，在独立 Docker 工作区中组织需求、代码、运行和人工确认。

M1—M4 已合并到 main。M5 首轮实现加入跨项目范围与联调登记、完整交付包、归档重开、资源限制及离线备份恢复。在线模型串联与用户业务项目仍待验收。

## 运行工作台

需要 Linux、Python 3.11+、Node.js 22.12+、Git 和 Docker：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
npm ci --prefix web
npm run --prefix web build
docker build -t agent-workbench-m1:local poc/container
.venv/bin/python -m agent_workbench.server
```

另开终端启动 Runner：

```bash
.venv/bin/python -m agent_workbench.runner
```

打开 [本机工作台](http://127.0.0.1:8340)，使用启动时提示的本机访问令牌文件登录。默认使用 SQLite；PostgreSQL、模型配置、代理证书及验证命令见 [M2 操作说明](docs/06-m2-workbench.md)。访问令牌与模型密钥分别管理，均不提交 Git。

默认模型沿用 M1 已验证的 OpenRouter 免费 Nemotron，Runner 从现有 `OPENROUTER_API_KEY` 环境变量读取凭据。可通过 API 与 Runner 一致的 `AWB_MODEL` 切换显式免费模型，不使用付费回退。

## 文档导航

1. [需求文档](docs/01-requirements.md)：业务流程、功能、范围、状态及验收标准。
2. [整体设计](docs/02-overall-design.md)：架构、模块、数据模型、工作区、隔离、执行协议、状态和 API。
3. [实施计划](docs/03-implementation-plan.md)：技术验证、迭代、工作包及需求追踪。
4. [M1 操作说明](docs/04-m1-runbook.md)：运行命令、真实模型验证及原型边界。
5. [M1 验证报告](docs/05-m1-validation.md)：已通过检查、实际版本及尚待完成的项目。
6. [M2 操作说明](docs/06-m2-workbench.md)：架构落地、启动、配置和接口。
7. [M2 验证报告](docs/07-m2-validation.md)：浏览器、数据库、迁移、恢复及验证范围。
8. [M3 流程说明](docs/08-m3-workflow.md)：默认步骤、需求修订、独立评审、问题处置及历史记录。
9. [M3 验证报告](docs/09-m3-validation.md)：受控流程、真实容器边界及在线模型待验收项。
10. [M4 材料说明](docs/10-m4-materials.md)：材料版本、主管登记、条件性能单、测试记录与反馈。
11. [M4 验证报告](docs/11-m4-validation.md)：材料闭环、历史下载、数据库并发及升级的验证边界。
12. [M5 交付与恢复](docs/12-m5-delivery.md)：跨项目范围、联调登记、完整交付、资源和维护命令。
13. [M5 验证报告](docs/13-m5-validation.md)：双项目交付、全量恢复、并发验证及真实业务待验收项。

## 运行 M1

```bash
.venv/bin/python -m unittest discover -s tests -v
docker build -t agent-workbench-m1:local poc/container
PYTHONPATH=src python3 poc/verify_isolation.py --root .local-data/m1-run-01
```

运行需要 Linux、Python 3.11+、Git 和可访问的 Docker 守护进程。代理环境的证书配置及免费模型真实任务命令见操作说明。以上双容器检查不会调用模型。

## 设计示例

- [项目环境配置示例](design-examples/project-profile.example.yaml)
- [开发流程配置示例](design-examples/workflow.example.yaml)

示例使用工作台拟设计的配置结构，仅用于评审。不是 Docker Compose、OpenCode 或 Superpowers 原生配置，当前没有解析器，不能直接运行。占位版本、脚本、挂载和模型引用需要在实施时替换。

推荐先阅读需求与设计，再按操作说明运行本地工作台。创建工作区后，在“开发流程”开启默认流程；自审通过后，在“设计、测试与反馈”开始材料阶段。多项目需求先在“跨项目交付”关联各项目并确认整体范围，最后汇总验收并交付。“资源与恢复”提供当前资源及离线维护说明。用户业务项目的具体环境仍需在试点接入时配置。
