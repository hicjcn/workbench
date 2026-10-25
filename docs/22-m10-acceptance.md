# M10 完整样例验收工具

版本 0.10.0，SQL schema 仍为 `0006`。PR #9 已合并，提交 `91fee0c2a3782071a4e84d2972a06b36f48a4163`。**当前交付的是可继续运行的验收工具，完整在线验收仍待完成。** 本轮云环境对 `opencode.ai` 和 `models.dev` 的代理 CONNECT 均返回 403，没有提交模型推理任务。

## 在本机准备

使用源码检出、Linux、Python 3.11+、Node 22、Docker 和已构建的固定工具镜像。按 README 安装 `.[server]` 并构建 Web。验收命令也可使用 `agent-workbench-acceptance`，升级安装后入口生效。

建议使用独立的工作台数据目录，以免混入业务项目。下面两个服务须使用相同的开发模型配置；模型 ID 只是已适配免费路径的示例，其当前可用性需实测。

```bash
# 终端一：API
AWB_MODEL=opencode/glm-5.3-flash-free .venv/bin/python -m agent_workbench.server \
  --data-dir .local-data/m10-workbench

# 终端二：Runner
AWB_MODEL=opencode/glm-5.3-flash-free .venv/bin/python -m agent_workbench.runner \
  --data-dir .local-data/m10-workbench
```

在同一主机、与 Runner 一致的网络及认证环境中运行诊断：

```bash
.venv/bin/python -m agent_workbench.acceptance doctor \
  --review-model opencode/glm-5.3-flash-free \
  --review-model opencode/big-pickle
```

诊断只请求固定公共端点，不发送提示词，不打印凭据、响应正文或原始网络错误。它区分代理拒绝、TLS 错误、HTTP 错误与连接失败。`NETWORK_REACHABLE_NOT_INFERRED` 只表示可以尝试，不能当作模型验证成功；网络阻断或缺少本进程的 OpenRouter 环境凭据时返回退出码 2。已有 OpenRouter 免费模型可使用原有环境变量引用，CLI 与 Runner 需各自正确配置；不在聊天或命令行参数中发送密钥。

初始化一个尚不存在的独立验收目录：

```bash
.venv/bin/python -m agent_workbench.acceptance setup \
  --root .local-data/m10-sample \
  --token-file .local-data/m10-workbench/access-token \
  --review-model opencode/glm-5.3-flash-free \
  --review-model opencode/big-pickle
```

默认 API 是 `http://127.0.0.1:8340`，可用 `--api` 改为同主机的其他 HTTP 端口。工具只允许本机根地址，不接受远程地址、URL 用户认证或明文令牌参数。请保持同一目录的 API 地址和角色模型一致。

初始化注册一个源码项目、固定运行配置、需求及单成员交付组。场景要求规范名字空白、中英文问候、未知语言回退，以及 HTTP 查询与函数使用一致规则。它复用现有流程，不创建审批身份或测试通过记录。

## 逐步执行

```bash
.venv/bin/python -m agent_workbench.acceptance advance \
  --root .local-data/m10-sample --token-file .local-data/m10-workbench/access-token

.venv/bin/python -m agent_workbench.acceptance status \
  --root .local-data/m10-sample --token-file .local-data/m10-workbench/access-token
```

每次 `advance` 最多提交一个操作，立即返回其 ID。任务运行中返回 `WAITING_JOB`，再次执行不会重复提交。命令按以下顺序选择下一步：

| 阶段 | 自动选择的机器操作 | 需要人在界面完成 |
| --- | --- | --- |
| 环境 | 准备工作区、只读预检、启动环境 | 处理实际环境错误 |
| 初始负例 | 执行固定验收测试，确认原始实现确实失败 | 若不是预期的测试失败，检查现场 |
| 分析 | 实际联合分析、开启单项目流程、实际分析 | 确认项目方案及整体范围 |
| 开发与评审 | 实际开发、固定命令验证、分别调用评审模型 | 逐项处理问题、人工自审 |
| 材料 | 开启材料轮次、生成概要设计及测试定义 | 登记真实主管意见、性能判断与逐项实际测试结果 |
| 反馈与交付 | 生成当前材料导出、采集已满足门禁的交付包 | 实际提出反馈、修复后的复评复测、反馈处置、联调登记及最终签收 |

`WAITING_HUMAN` 会标明具体确认点。工具不会提交范围批准、自审、主管通过、性能豁免、测试 PASS、反馈解决或材料验收。单项目场景可由人说明为何无需跨服务联调，不能由工具代填该决定。

在线步骤前重新检查固定模型的公共端点。诊断阻断时不提交模型任务；没有付费回退、伪造模型回复或自动重试。失败、取消、UNKNOWN 与失效流程都会暂停。网络恢复后继续使用同一目录；已失败任务只有在检查现场后才能明确重试：

```bash
.venv/bin/python -m agent_workbench.acceptance advance \
  --root .local-data/m10-sample --token-file .local-data/m10-workbench/access-token \
  --retry-job job_实际失败任务的32位标识
```

UNKNOWN 必须先在界面停止并核实。初始负例失败是预期条件，工具核对真实 TAP 失败和退出码后会自动继续到环境恢复，无需重试这一负例。初始实现意外通过、TAP 输出不足或测试执行方式改变时，工具不会把它计为有效负例。

## 固定测试与请求恢复

独立验收文件保存在 `<root>/criteria/acceptance.test.mjs`，以只读配置挂载到容器 `/run/workbench-config/acceptance.test.mjs`。固定命令是 `node --test --test-reporter=tap /run/workbench-config/acceptance.test.mjs`。六项用例包含函数与实际 HTTP 服务测试，位于 Agent 可写源码之外。工具固定原始样例、测试摘要、运行配置和角色模型；变化后不能静默沿用旧结论。

每个 POST 前先保存唯一请求键及请求意图。网络响应丢失、服务端 5xx 或响应缺少资源 ID 时保留意图，不自动重发。先在界面确认实际资源或任务 ID，再显式核实：

```bash
.venv/bin/python -m agent_workbench.acceptance resolve \
  --root .local-data/m10-sample --token-file .local-data/m10-workbench/access-token \
  --resource-id 实际资源或任务ID
```

任务需匹配原请求键与本需求，注册资源需匹配已保存的请求属性。`resolve` 只读取并核对现有对象，不重新创建资源。多个本机命令使用同一目录的文件锁；`acceptance.json`、`model-readiness.json` 和 `last-result.json` 是本地私有运行记录。

## 验收证据

需求页新增“完整流程验收证据”，也可读取 `GET /api/v1/requirements/{id}/acceptance`。它核对已保存的模型结束事件、模型 ID、文本结果、技能读取或调用、当前验证、独立评审、材料生成与人工登记。只填写成功状态或保存受控报告，不会满足在线模型证据项。

执行器在实际观察到明确结束且没有错误后新增 `model_completion` 元数据，保存模型 ID、文本存在标记、已完成的已知技能操作及脱敏流摘要。这样长正文在展示日志中被截断时，仍有可核对的完成记录。技能名称出现在说明文字中不计为工具使用；记录只证明可观察到的技能操作，不证明完整开发方法的质量。

清单状态是 `INCOMPLETE` 或 `EVIDENCE_RECORDED_REQUIRES_REVIEW`，不会自动宣布全部 MVP 或业务交付通过。主管和测试项目明确标为人工登记，接口不独立认证人员或模型服务身份。至少一次反馈后的复评复测也是完整样例的待签收项；Bug、需求变更及架构变更可分别演练并保留原历史。

离线数据备份继续使用 v2。外部验收目录中的原始样例、固定测试和 CLI 日志不进入工作台数据备份，需一同保管。恢复工作台到新目录后使用新令牌，并保留原验收目录及 API 地址；不会自动重放任务或推理。当前云端只有验收工具与阻断证据，在线链路需在可访问模型的环境中完成。
