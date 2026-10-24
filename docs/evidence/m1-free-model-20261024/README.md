# 免费模型任务证据：2026-10-24

run-01、run-02 使用 `opencode/glm-5.3-flash-free`；最新 run-03 按用户要求改用 `opencode/mimo-v2.6-flash-free`。各轮均未创建或注入模型凭据，未使用付费模型。run-01 被代理 CONNECT 403 阻塞；run-02、run-03 服务可达，但均收到免费额度耗尽响应，无模型输出或代码修改。三轮都不能登记为模型开发验证通过。

## run-03：MiMo-V2.6-Flash Free

在 `b0e5be195e710aade9e356e480d34d3f02599256` 上使用同一脚本、提示词、样例基线和验收条件，仅显式改用 `--model opencode/mimo-v2.6-flash-free`。2026-10-24 北京时间 10:42:08 至 10:43:00，共 52.132 秒。新会话提交一次后同样收到 `free_tier_limit`；记录状态后中止等待，未重放任务。

- [model-catalog.json](run-03/model-catalog.json)：公开目录 HTTP 200，准确模型 ID 为 `mimo-v2.6-flash-free`。
- [model-message-summary.json](run-03/model-message-summary.json)：只读采集的消息元数据确认实际使用 `opencode / mimo-v2.6-flash-free`，观察时 cost 与 token 均为 0，无模型内容片段。
- [model-session-status.json](run-03/model-session-status.json) / [session-status-observations.jsonl](run-03/session-status-observations.jsonl)：服务端额度原因、会话 ID 和下一次重试时间（2026-10-25 北京时间 08:00，不保证恢复）。
- [model-abort.json](run-03/model-abort.json) / [cleanup.json](run-03/cleanup.json)：本次会话中止成功，结束后无托管容器或网络。
- [report.json](run-03/report.json) / [verification-output.txt](run-03/verification-output.txt)：原始报告与阶段输出，状态 `FAILED_OR_BLOCKED_MODEL_TASK`，脚本退出码 1。
- [execution-context.json](run-03/execution-context.json) / [approved-task.txt](run-03/approved-task.txt)：执行版本和免凭据状态、实际提交任务；未读取认证内容。
- [before-snapshot.json](run-03/before-snapshot.json) / [after-snapshot.json](run-03/after-snapshot.json) / [code-diff.patch](run-03/code-diff.patch)：前后摘要相同，代码差异为空。
- [test-output.txt](run-03/test-output.txt) / [acceptance-output.txt](run-03/acceptance-output.txt)：仅原始 2 项测试通过，空白处理的独立验收失败。
- [agent-events.jsonl](run-03/agent-events.jsonl) / [agent-stderr.txt](run-03/agent-stderr.txt)：均为空；CLI 返回 0，额度原因单独从 API 采集。
- [sha256.json](run-03/sha256.json)：其余 17 份证据文件的 SHA-256。

MiMo 和 GLM 返回相同额度原因，不据此推断服务端具体计额维度。没有模型代码产出，也没有人工修改样例代替模型产出。

## run-02：免费额度阻塞

在 `c566bbfe5fdfee5412b4dc8c635de7df5842c308` 上运行 `poc/verify_free_model.py`，2026-10-24 北京时间 10:34:47 至 10:37:34，共 167.530 秒。一次提示词提交后，控制 API 返回 `free_tier_limit`，下一次重试安排在 2026-10-25 北京时间 08:00；保存状态后中止此次等待，让脚本继续验收与清理，没有重放任务。

| 证据 | 内容 |
| --- | --- |
| [report.json](run-02/report.json) / [verification-output.txt](run-02/verification-output.txt) | 脚本原始报告和阶段输出；`FAILED_OR_BLOCKED_MODEL_TASK`，脚本退出码 1，源仓库不变、容器已停止 |
| [model-session-status.json](run-02/model-session-status.json) | 执行期间只读采集 `/session/status`，包含免费额度原因、服务端下一次重试时间和会话 ID |
| [model-abort.json](run-02/model-abort.json) | 中止本次会话的原因、API 返回 `true` 及中止后的空状态 |
| [execution-context.json](run-02/execution-context.json) | 工作台提交、固定工具版本与镜像 ID；只检查模型认证文件是否存在，未读取认证内容 |
| [network-probe.json](run-02/network-probe.json) | 同一代理与 TLS 校验下，默认 Python 客户端收到 HTTP 403；OpenCode 客户端标识返回 200，目录包含指定模型 |
| [approved-task.txt](run-02/approved-task.txt) | 实际提交的空白处理与测试任务 |
| [before-snapshot.json](run-02/before-snapshot.json) / [after-snapshot.json](run-02/after-snapshot.json) | 前后快照，源码摘要相同 |
| [test-output.txt](run-02/test-output.txt) | 仅原始 2 项测试通过，无新增回归测试 |
| [acceptance-output.txt](run-02/acceptance-output.txt) | 独立行为验收失败：带空白名称未被修剪 |
| [code-diff.patch](run-02/code-diff.patch) | 空文件，未产生代码修改 |
| [agent-events.jsonl](run-02/agent-events.jsonl) / [agent-stderr.txt](run-02/agent-stderr.txt) | 均为空；CLI 返回 0，未输出模型响应或错误事件，额度原因来自上面的实时 API 记录 |
| [cleanup.json](run-02/cleanup.json) | 脚本结束后复核无托管容器或网络 |
| [sha256.json](run-02/sha256.json) | 本目录其余证据文件的 SHA-256 |

JSON 时间使用 UTC。`report.json` 保留脚本原样，没有将外部 API 观察补写为 CLI 事件；其中能力探测是提交前状态，任务是否提交看顶层字段。没有以人工实现修补样例，也没有将空差异或原始测试解释为模型开发成功。

## run-01：历史代理阻塞

[network-probe.json](network-probe.json) 保存同一环境经原有代理探测两个主域名的结果和检查时间；保持 TLS 校验。

`run-01/` 是在 `c7aa01a` 上运行原验证脚本收集的输出，保留原格式，未补写后来新增的报告字段：

- [report.json](run-01/report.json)：`FAILED_OR_BLOCKED_MODEL_TASK`、源仓库不变、容器已停止。
- [agent-events.jsonl](run-01/agent-events.jsonl)：唯一事件为代理 403 错误；CLI 退出码为 0。
- [test-output.txt](run-01/test-output.txt)：原始 2 项测试通过，未新增回归测试。
- [code-diff.patch](run-01/code-diff.patch)：空文件，表示未产生代码差异。
- [approved-task.txt](run-01/approved-task.txt)：实际提交的任务和验收要求。
- [agent-stderr.txt](run-01/agent-stderr.txt)：空文件；错误通过 JSON 事件输出。

各轮原始目录为 `.local-data/free-model-run-01`、`.local-data/free-model-run-02`、`.local-data/free-model-run-03`，未提交包含控制密码、认证数据或 Agent 主目录的内容。后续验证应使用新根目录，保持历史记录不变。
