# Agent 开发工作台

面向后端开发者的需求与整体设计资料。以 OpenCode 为主要执行工具，结合 Superpowers，在独立 Docker 工作区中组织分析、开发、评审、文档和测试反馈。

当前阶段为设计，尚未实现应用或执行真实项目验证。

## 文档导航

1. [需求文档](docs/01-requirements.md)：业务流程、功能、范围、状态及验收标准。
2. [整体设计](docs/02-overall-design.md)：架构、模块、数据模型、工作区、隔离、执行协议、状态和 API。
3. [实施计划](docs/03-implementation-plan.md)：技术验证、迭代、工作包及需求追踪。

## 设计示例

- [项目环境配置示例](design-examples/project-profile.example.yaml)
- [开发流程配置示例](design-examples/workflow.example.yaml)

示例使用工作台拟设计的配置结构，仅用于评审。不是 Docker Compose、OpenCode 或 Superpowers 原生配置，当前没有解析器，不能直接运行。占位版本、脚本、挂载和模型引用需要在实施时替换。

推荐先阅读需求，再审阅整体设计中的默认选择，随后按实施计划 M1 完成工具及环境验证。技术栈为暂定参考方案，用户实际项目、工具版本及部署信息见实施计划的信息清单。
