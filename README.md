# Agent 开发工作台

面向后端开发者的需求与整体设计资料。以 OpenCode 为主要执行工具，结合 Superpowers，在独立 Docker 工作区中组织分析、开发、评审、文档和测试反馈。

当前处于 M1 技术验证阶段，已提供可运行的 Python CLI、Docker/OpenCode 环境及双需求隔离验证。完整工作台尚未实现，真实模型任务的当前状态见验证报告。

## 文档导航

1. [需求文档](docs/01-requirements.md)：业务流程、功能、范围、状态及验收标准。
2. [整体设计](docs/02-overall-design.md)：架构、模块、数据模型、工作区、隔离、执行协议、状态和 API。
3. [实施计划](docs/03-implementation-plan.md)：技术验证、迭代、工作包及需求追踪。
4. [M1 操作说明](docs/04-m1-runbook.md)：运行命令、真实模型验证及原型边界。
5. [M1 验证报告](docs/05-m1-validation.md)：已通过检查、实际版本及尚待完成的项目。

## 运行 M1

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
docker build -t agent-workbench-m1:local poc/container
PYTHONPATH=src python3 poc/verify_isolation.py --root .local-data/m1-run-01
```

运行需要 Linux、Python 3.11+、Git 和可访问的 Docker 守护进程。代理环境的证书配置及免费模型真实任务命令见操作说明。以上双容器检查不会调用模型。

## 设计示例

- [项目环境配置示例](design-examples/project-profile.example.yaml)
- [开发流程配置示例](design-examples/workflow.example.yaml)

示例使用工作台拟设计的配置结构，仅用于评审。不是 Docker Compose、OpenCode 或 Superpowers 原生配置，当前没有解析器，不能直接运行。占位版本、脚本、挂载和模型引用需要在实施时替换。

推荐先阅读需求，再审阅整体设计中的默认选择，随后按实施计划 M1 完成工具及环境验证。技术栈为暂定参考方案，用户实际项目、工具版本及部署信息见实施计划的信息清单。
