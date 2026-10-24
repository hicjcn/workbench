# OpenRouter 接入准备：2026-10-24

用户指定 `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`，密钥来源为用户现有 `OPENROUTER_API_KEY` Secret。本目录只记录代码接入、自动测试、真实容器配置检查和当前实例运行条件，不代表真实模型开发通过。

- [unit-test-output.txt](unit-test-output.txt)：29 项 Python 测试通过，包括 provider/免费模型选择、完整零价格门槛、缺失密钥、付费目录拦截、环境注入与持久配置覆盖、正常/超时证据脱敏及网络错误信息保护。
- [sample-test-output.txt](sample-test-output.txt)：未修改的样例项目原始 2 项测试通过。
- [configuration-smoke.json](configuration-smoke.json)：OpenCode 1.19.4 真容器的 `PASSED_CONFIGURATION_ONLY`。使用无效测试占位符，不使用真实凭据、不发推理任务；主模型、辅助模型、唯一 provider 与唯一模型白名单均为用户指定值。密钥值未进入生成配置或运行状态，未创建模型认证文件，容器已停止。
- [readiness.json](readiness.json)：北京时间 2026-10-24 11:00:23 当前实例检查；密钥变量不存在、白名单无 OpenRouter 域名，经继承代理访问公开目录收到 CONNECT 403，保持 TLS 校验。真实模型任务未提交。
- [cleanup.json](cleanup.json)：检查结束后无托管容器或网络遗留。
- [sha256.json](sha256.json)：以上 5 份原始证据文件的 SHA-256。

JSON 时间使用 UTC。用户已回复变量就绪，但当前实例观察不支持“变量已注入”；需将已保存 Secret 和 `openrouter.ai` 规则应用到该实例后再运行。完整模型 ID 已确定，无需再次提供密钥文本或创建新密钥。公开 OpenRouter 目录的所选模型价格与真实推理结果仍待网络放行后核实。

未复制 Agent 主目录、运行状态、完整 Docker 配置或认证内容。无效测试占位符仅用于证明配置读取与注入路径；不能用于真实调用，也没有被登记为用户密钥。
