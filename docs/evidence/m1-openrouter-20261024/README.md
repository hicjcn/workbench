# OpenRouter 接入与真实任务：2026-10-24

用户指定 `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`，密钥来源为用户现有 `OPENROUTER_API_KEY` Secret。新实例的 run-06 已完成真实模型代码修改及验收；根目录保留此前接入阶段的配置检查和旧实例阻塞证据，不覆盖历史记录。

## run-06：真实模型任务通过

执行提交 `4b4eaea763a3f653c5b12a794da4704c506fdee0`，时间为 2026-10-24 北京时间 11:14:10 至 11:15:04，耗时 53.995 秒。只提交一次，未超时、未重放，没有创建模型凭据或配置付费回退。run-05 已用于 GLM，因此 OpenRouter 使用新目录 run-06。

- [readiness.json](run-06/readiness.json)：Secret `ready`、变量非空、网络规则 `enforced`，公开目录可达，指定模型 prompt/completion 价格均为 0；保留继承代理和 TLS 校验。
- [report.json](run-06/report.json) / [verification-output.txt](run-06/verification-output.txt) / [verification-exit-code.txt](run-06/verification-exit-code.txt)：原始结果 `PASSED_REAL_MODEL_TASK`，脚本和 CLI 均退出 0，模型响应、实现变化、测试变化、4 项测试、源仓库不变及独立行为验收门槛全部通过。
- [approved-task.txt](run-06/approved-task.txt)：实际提交的空白处理及回归测试任务；禁止子 Agent、推送、工具网络访问和容器创建。
- [agent-events.jsonl](run-06/agent-events.jsonl) / [agent-stderr.txt](run-06/agent-stderr.txt) / [event-summary.json](run-06/event-summary.json)：脱敏事件和标准错误；20 条事件，6 个完成步骤的 cost 均为 0，最终 reason 为 `stop`。
- [before-snapshot.json](run-06/before-snapshot.json) / [after-snapshot.json](run-06/after-snapshot.json) / [code-diff.patch](run-06/code-diff.patch)：模型修改 `server.mjs` 和 `server.test.mjs`，实现 trim/空白回退，新增两个回归测试；摘要变化，源仓库保持不变。未人工修改样例。
- [test-output.txt](run-06/test-output.txt) / [acceptance-output.txt](run-06/acceptance-output.txt)：副本 4 项测试全部通过；脚本另行执行的三个行为断言通过，验收输出为空且退出成功。
- [runtime-model-configuration.json](run-06/runtime-model-configuration.json)：仅保存主模型、辅助模型、唯一 provider 和唯一白名单，均为用户指定的免费模型，不复制完整配置或密钥。
- [model-message-summary.json](run-06/model-message-summary.json) / [session-status-observations.jsonl](run-06/session-status-observations.jsonl)：运行中的 API 观察；确认实际 provider/model、非零 token 和工具调用，此摘要不冒充最终事件流。
- [execution-context.json](run-06/execution-context.json)：镜像和固定工具版本、执行提交、已有凭据来源，以及未创建模型认证文件；没有凭据值。
- [unit-test-output.txt](run-06/unit-test-output.txt) / [sample-test-output.txt](run-06/sample-test-output.txt)：31 项 Python 回归与仓库原始 2 项 Node.js 测试通过；模型副本的 4 项测试见上面的独立文件。
- [cleanup.json](run-06/cleanup.json)：无托管容器、网络、运行状态及模型认证文件遗留。
- [sha256.json](run-06/sha256.json)：其余 20 份证据文件的 SHA-256。

本地原始目录 `.local-data/free-model-run-06` 保留且不提交。证据只复制明确列出的文件并脱敏，未复制 Agent 主目录、运行状态、完整 Docker 配置或认证内容。原始报告内 `capabilities.model_execution=NOT_ATTEMPTED` 来自提交前探测；顶层 `submission_attempted=true` 和最终状态记录真实任务。此次小样例验收通过不代表完整工作台完成。

## 历史：接入准备与旧实例阻塞

以下根目录记录只代表接入阶段，不代表真实模型开发通过；原始文件和校验清单保持原样。

- [unit-test-output.txt](unit-test-output.txt)：29 项 Python 测试通过，包括 provider/免费模型选择、完整零价格门槛、缺失密钥、付费目录拦截、环境注入与持久配置覆盖、正常/超时证据脱敏及网络错误信息保护。
- [sample-test-output.txt](sample-test-output.txt)：未修改的样例项目原始 2 项测试通过。
- [configuration-smoke.json](configuration-smoke.json)：OpenCode 1.19.4 真容器的 `PASSED_CONFIGURATION_ONLY`。使用无效测试占位符，不使用真实凭据、不发推理任务；主模型、辅助模型、唯一 provider 与唯一模型白名单均为用户指定值。密钥值未进入生成配置或运行状态，未创建模型认证文件，容器已停止。
- [readiness.json](readiness.json)：北京时间 2026-10-24 11:00:23 当前实例检查；密钥变量不存在、白名单无 OpenRouter 域名，经继承代理访问公开目录收到 CONNECT 403，保持 TLS 校验。真实模型任务未提交。
- [cleanup.json](cleanup.json)：检查结束后无托管容器或网络遗留。
- [sha256.json](sha256.json)：以上 5 份原始证据文件的 SHA-256。

JSON 时间使用 UTC。此前用户已回复变量就绪，但旧实例观察不支持“变量已注入”；这是历史阻塞原因，新实例就绪及真实任务结果见上面的 run-06。

未复制 Agent 主目录、运行状态、完整 Docker 配置或认证内容。无效测试占位符仅用于证明配置读取与注入路径；不能用于真实调用，也没有被登记为用户密钥。
