# M1 验证报告

- 日期：2026-10-24
- 状态：指定 OpenRouter 免费模型的真实代码修改、4 项测试和独立行为验收已通过；完整工作台及任务恢复仍待后续迭代
- 范围：内置 Node.js 测试项目，不包含用户业务项目或生产数据

## 1. 已完成结果

| 检查 | 结果 | 证据与边界 |
| --- | --- | --- |
| 代码工作区、控制接口与模型验收测试 | 通过 | 本轮 31 项自动测试；保留 OpenRouter 零价格、密钥注入和脱敏检查，并补充 OpenCode 免费模型的主/辅助模型及白名单固定检查 |
| 测试项目 | 通过 | 仓库原始 2 项测试通过；真实模型在独立副本补充后 4 项测试通过 |
| 镜像构建 | 通过 | 固定 OpenCode 及 Superpowers 版本实际构建成功 |
| 双容器启动 | 通过 | 两需求分别具有网络、控制端口、代码和 Agent 主目录 |
| Superpowers 加载 | 通过 | 原生技能发现包含 brainstorming、writing-plans、using-superpowers 等 |
| 控制 API | 通过 | 健康、OpenAPI、空会话创建、中止及删除 |
| 容器代码写入隔离 | 通过 | 需求 A 改动后，需求 B 和原始项目保持不变 |
| 两容器内检查 | 通过 | 分别执行测试项目的 Node 测试 |
| 重建持久化 | 通过 | 需求 A 重建后代码和 Agent 主目录标记保留 |
| 重建互不影响 | 通过 | 需求 A 重建期间和之后，需求 B 的控制 API 保持健康 |
| 资源清理 | 通过 | 双容器验证报告 cleanup_errors 为空 |
| OpenRouter 配置检查 | 通过 | 固定版本 OpenCode 真容器、无效测试占位符；仅启用 OpenRouter，主/辅助模型与白名单固定为指定免费模型；无真实推理 |
| 免费模型真实任务 | 通过 | [OpenRouter run-06](evidence/m1-openrouter-20261024/run-06/report.json) 为 `PASSED_REAL_MODEL_TASK`：模型修改实现和测试，4 项测试及独立行为验收通过，源仓库不变 |

双容器脚本的实际结果为 `PASSED_INFRASTRUCTURE_ONLY`，6 个集成检查通过。该状态不包含模型推理或完整工作台验收。

## 2. 实际版本与运行边界

| 项目 | 观察值 |
| --- | --- |
| Docker Engine | 28.4.0 |
| Python | 3.12.14 |
| OpenCode 健康接口版本 | 1.19.4 |
| Superpowers | `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`，v7.0.1 |
| 本轮验证镜像 ID | `sha256:d31046286a926a3e6de0f4b85a397f9ce643ce790d89cb5ea6f759a831175b9d`；历史双容器及模型轮次保留各自镜像记录 |
| 容器用户 | 1000:1000 |
| 根文件系统 | 只读 |
| 特权模式 | 关闭 |
| Docker Socket 挂载 | 无 |
| 严格出站限制 | 原型未实现，不宣称具备该能力 |

## 3. 实测发现与修复

第一次环境重建时，OpenCode 启动窗口出现 HTTP 连接重置。原控制客户端仅处理部分连接异常，导致探测过早失败。

已将连接重置等传输异常归为可重新探测的控制错误，增加对应回归测试。修复后第二轮双容器验证全部通过，原失败记录保存在被 Git 忽略的本地证据目录。

随后补充持久化 Agent 配置目录的软链接校验及私有临时文件原子写入，防止容器内持久化内容在宿主机重建时被当成可信路径。16 项回归测试及第三轮真实双容器验证全部通过。

## 4. 历史 OpenCode 免凭据模型验证

run-01 至 run-04 先后使用 `opencode/glm-5.3-flash-free`、`opencode/mimo-v2.6-flash-free` 和 `opencode/deepseek-v4.1-flash-free`。三者均由容器的 `opencode models opencode` 和 [官方模型目录](https://opencode.ai/zen/v1/models) 实际列出。各轮均不创建或注入模型凭据，不配置付费回退。

真实模型验证脚本的完成条件同时要求：任务无错误、观察到模型响应、`server.mjs` 实际变化、回归测试文件变化、至少 4 项测试通过、源仓库不变，以及独立验收行为通过。目录可达、CLI 返回 0 或原始测试通过均不足以判定真实任务成功。

### 上一轮：代理拒绝（run-01）

在 `c7aa01a` 上运行时，任务收到 `Proxy refused to open a tunnel: 403 Forbidden`，无模型响应或代码变化。CLI 返回 0，验证脚本正确返回 `FAILED_OR_BLOCKED_MODEL_TASK`。原始 [run-01 报告](evidence/m1-free-model-20261024/run-01/report.json) 和 [代理探测](evidence/m1-free-model-20261024/network-probe.json) 保持原样。

随后 `c566bbf` 加强验证器的响应和实现变化门槛、前后快照及超时证据保留，20 项回归测试通过。这些是验证器改动，不能作为 GLM 生成业务代码的证据。

### 上一轮：GLM 服务端免费额度耗尽（run-02）

在 `codex/m1-technical-validation` 的 `c566bbfe5fdfee5412b4dc8c635de7df5842c308` 上重建相同固定版本镜像后，实际执行一次：

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-02 \
  --model opencode/glm-5.3-flash-free \
  --proxy-ca "$CODEX_PROXY_CERT" --timeout 600
```

执行时间为 2026-10-24 北京时间 10:34:47 至 10:37:34，耗时 167.530 秒；原始 JSON 时间使用 UTC。基础设施已通过，本轮未重复双容器隔离验证。

| 观察项 | 实际结果 |
| --- | --- |
| OpenCode / Superpowers / Node.js | 1.19.4 / v7.0.1（`2ce8aa1`）/ v22.23.4 |
| 本轮镜像 ID | `sha256:b56ce1378a26dea06cf3d79bae1f70694db26ec2ac3b91e4f50ffba31a34664e`；区别于第 2 节和 run-01 历史镜像 |
| 网络探测 | 继承代理、保留 TLS 校验；使用 `User-Agent: opencode/1.19.4` 时两个服务均返回 HTTP 200，模型目录包含指定模型 |
| 指定模型已列出 / 任务已提交 | 是 / 是；提交一次，未重放提示词，未创建模型凭据或使用付费回退 |
| 服务端状态 | `/session/status` 返回 `type=retry`、`attempt=1`、`action.reason=free_tier_limit`、`Free usage exceeded, subscribe to Go` |
| 服务端给出的下次重试时间 | 2026-10-25 北京时间 08:00:00（原值 `1792886400257` 毫秒）；不保证届时额度恢复 |
| 中止等待 | 已调用本次会话的 abort，返回 `true`；随后会话状态为空，未等待次日重试 |
| CLI 退出码 / JSON 事件 | 0 / 无事件；额度原因由执行期间的控制 API 单独采集 |
| 模型响应 / 实现变化 / 回归测试变化 | 均为否；前后源码快照相同，代码差异为空 |
| 原有测试 / 独立行为验收 | 2 项通过 / 失败：实际 `hello   developer  `，期望 `hello developer` |
| 原始源码 | 保持不变；前后摘要均为 `e378190b5ac1234c300e3ba6dd7bbb2e3f7210c3064e1fca92267818d52dcb19` |
| 最终状态 | `FAILED_OR_BLOCKED_MODEL_TASK`；脚本退出码 1，未登记真实代码修改通过 |
| 清理 | `runtime_stopped=true`；另行复核无托管容器或网络遗留 |

默认 Python 客户端探测同样地址时收到 HTTP 403（响应 `error code: 1010`）；携带上述 OpenCode 客户端标识时返回 200。本轮实际模型会话已获得服务端额度响应，因此不能沿用上一轮的代理 CONNECT 403 诊断。

完整可评审记录见 [证据索引](evidence/m1-free-model-20261024/README.md)：新增 run-02 的任务文本、原始报告、前后快照、测试与验收输出、空差异、额度状态、中止记录、网络探测、执行版本及清理复核。保留脚本报告原样，没有将 API 采集的状态补写成 CLI 错误事件；其中 `capabilities.model_execution=NOT_ATTEMPTED` 来自提交前的基础设施探测，实际提交状态看顶层 `submission_attempted=true`。本地原始目录 `.local-data/free-model-run-02` 不提交；Agent 主目录、控制密码及认证数据不进入公开证据。

本轮复核 20 项 Python 测试及原始 2 项 Node.js 测试通过。免费额度阻塞使真实模型代码修改、新增回归测试及行为验收仍未完成；本轮没有用人工修改替代模型产出。

### 上一轮：MiMo-V2.6-Flash Free（run-03）

用户要求改用 MiMo-V2.6-Flash Free。公开目录确认准确 ID 为 `mimo-v2.6-flash-free`，在 `b0e5be195e710aade9e356e480d34d3f02599256` 上实际执行一次：

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-03 \
  --model opencode/mimo-v2.6-flash-free \
  --proxy-ca "$CODEX_PROXY_CERT" --timeout 600
```

执行时间为 2026-10-24 北京时间 10:42:08 至 10:43:00，耗时 52.132 秒。沿用 run-02 的验证镜像及固定工具版本，仅显式更换模型参数，未修改任务、测试项目基线或验收条件。

| 观察项 | 实际结果 |
| --- | --- |
| 模型目录 / 容器模型列表 | HTTP 200，包含 `mimo-v2.6-flash-free` / 指定模型已列出 |
| 实际会话模型 | 控制 API 消息元数据为 `providerID=opencode`、`modelID=mimo-v2.6-flash-free`；观察时 cost 和输入/输出 token 均为 0，无内容片段 |
| 服务端状态 | `type=retry`、`attempt=1`、`action.reason=free_tier_limit`、`Free usage exceeded, subscribe to Go` |
| 下次重试时间 | 2026-10-25 北京时间 08:00:00（原值 `1792886400338` 毫秒）；不保证届时额度恢复 |
| 提交与停止 | 新会话提交一次；采集额度状态后 abort 返回 `true`，随后会话状态为空；未重放任务 |
| CLI / 验证脚本退出码 | 0 / 1；CLI JSON 事件和标准错误均为空 |
| 代码与回归测试 | 无模型响应、实现变化或新增测试；前后摘要与原始源码均相同，差异为空 |
| 测试 / 独立行为验收 | 原始 2 项通过 / 失败，空白修剪未实现 |
| 结果 / 清理 | `FAILED_OR_BLOCKED_MODEL_TASK` / 容器已停止，复核无托管容器及网络 |

证据见 [run-03 索引](evidence/m1-free-model-20261024/README.md#run-03mimo-v26-flash-free)：新增模型目录、实际模型消息元数据、额度状态观察、原始报告、任务、前后快照、测试/验收输出、空差异、中止与清理记录及校验清单。未提交 Agent 主目录、认证数据或控制密码。

MiMo 与 GLM 均观察到相同的服务端免费额度原因，当前证据不能确定额度按模型还是其他维度计算。更换模型没有解除本轮阻塞，仍未完成真实模型代码修改与新行为验收。

### DeepSeek V4.1 Flash Free（run-04）

用户要求改试 DeepSeek V4.1 Flash Free。公开目录 HTTP 200，准确 ID 为 `deepseek-v4.1-flash-free`；在 `e05c988417e31d6ac91c5cf21d15daa6db13147c` 上实际执行一次：

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-04 \
  --model opencode/deepseek-v4.1-flash-free \
  --proxy-ca "$CODEX_PROXY_CERT" --timeout 600
```

执行时间为 2026-10-24 北京时间 10:48:24 至 10:48:49，耗时 24.889 秒。沿用同一镜像、固定工具版本、任务、样例基线和验收条件，仅显式更换模型参数。

控制 API 消息元数据确认为 `providerID=opencode`、`modelID=deepseek-v4.1-flash-free`；观察时 cost 与输入/输出 token 均为 0，无内容片段。会话状态同样返回 `type=retry`、`attempt=1`、`action.reason=free_tier_limit` 和 `Free usage exceeded, subscribe to Go`，服务端给出的下次重试时间为 2026-10-25 北京时间 08:00:00（原值 `1792886400715` 毫秒），不保证届时额度恢复。

记录额度状态后 abort 返回 `true`，随后会话状态为空。CLI 返回 0，事件和标准错误为空；未产生模型响应、实现或回归测试变化，前后快照及原始源码摘要相同，代码差异为空。仅原始 2 项测试通过，独立空白处理验收失败。脚本返回 `FAILED_OR_BLOCKED_MODEL_TASK`，退出码 1，容器及网络均已清理。本轮没有重放任务、创建模型凭据或使用付费模型。

新增 [run-04 证据](evidence/m1-free-model-20261024/README.md) 包含 17 份原始记录及 SHA-256 清单：模型目录、实际模型元数据、额度状态、任务、报告、前后快照、测试/验收输出、空差异、中止、执行版本及清理结果。GLM、MiMo、DeepSeek 三个免费模型均观察到相同额度原因；更换模型尚未解除阻塞，当前证据仍不能确定服务端计额维度，真实模型代码修改与行为验收保持未完成。

### GLM 再次验证与任务切换（run-05）

按本次对话最初指令，在新目录 `free-model-run-05` 提交一次 GLM 任务。会话再次返回 `free_tier_limit`，无模型响应或代码修改。用户随后明确固定使用 OpenRouter，因此中止 GLM 会话，abort 返回 `true`，脚本完成测试和资源清理，结果仍为 `FAILED_OR_BLOCKED_MODEL_TASK`。原始证据见 [run-05](evidence/m1-free-model-20261024/README.md#run-05glm-再次验证与任务切换)，未覆盖历史目录。

提交 `4b4eaea` 同时将 OpenCode 免费模型路径的主模型、辅助模型、唯一 provider 和白名单固定为显式选择的模型，重启时覆盖持久配置；新增两项测试并收紧模型 ID 检查。该验证器改动不是模型生成的样例实现，也不改变 OpenRouter 原有免费检查。

## 5. OpenRouter 接入与真实任务

用户提供模型 `nvidia/nemotron-3-ultra-550b-a55b:free`，要求改用 OpenRouter provider 和用户现有密钥。验证器默认模型已改为 `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`；历史 OpenCode 模型仍可通过显式参数复核，没有自动回退。

本轮实现：只接受 OpenRouter 的明确 `vendor/model:free` ID；提交前无认证查询公开模型目录，要求所选模型存在、prompt/completion 价格完整且所有价格项均为有限数值 0；缺失密钥、目录不可达、模型不存在或任何非零/未知价格均阻止任务提交。OpenCode 容器仅接收 `OPENROUTER_API_KEY` 环境变量，生成配置只保存环境变量引用；仅启用 OpenRouter，主模型、辅助模型和白名单都固定为同一免费模型，持久配置也会重设限制。事件、错误、测试与验收输出及代码差异在保存前替换已知密钥值，超时部分输出亦脱敏。

29 项 Python 回归测试和原始 2 项 Node.js 测试通过。固定 OpenCode 1.19.4 真容器仅做配置检查，使用无效测试占位符，不调用推理、不使用真实凭据；检查已配置的主/辅助模型、唯一 provider 和模型白名单，以及密钥值未写入生成配置或运行状态。该检查仅能登记为 `PASSED_CONFIGURATION_ONLY`，不能作为真实模型开发通过的证据。可评审记录见 [OpenRouter 接入证据](evidence/m1-openrouter-20261024/README.md)。

上一会话的实例检查为 `OPENROUTER_API_KEY` 不存在、无 Secret 绑定且 OpenRouter 目录收到 CONNECT 403。上述 [历史运行条件](evidence/m1-openrouter-20261024/readiness.json) 保留原样；它不代表下面新实例的状态。

### run-06：真实模型代码修改及验收通过

本次实例确认 `observations_current=true`、Secret 状态 `ready`、环境变量非空，网络规则为 `enforced` 且允许 `openrouter.ai`。继承代理和 TLS 校验访问公开目录成功；指定模型存在，返回的 prompt/completion 价格均为 0。只复用已有密钥，不输出值、不创建模型认证文件。

在提交 `4b4eaea763a3f653c5b12a794da4704c506fdee0` 上运行：

```bash
PYTHONPATH=src python3 poc/verify_free_model.py \
  --root .local-data/free-model-run-06 \
  --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free \
  --proxy-ca "$CODEX_PROXY_CERT" --timeout 600
```

`run-05` 已被 GLM 占用，因此选择新的 `run-06`。执行时间为 2026-10-24 北京时间 11:14:10 至 11:15:04，耗时 53.995 秒，提交一次，没有超时或重放，也未重复双容器隔离验证。

| 观察项 | 实际结果 |
| --- | --- |
| 实际 provider / 模型 | 控制 API 消息为 `openrouter / nvidia/nemotron-3-ultra-550b-a55b:free`，存在非零 token 和工具调用 |
| 运行配置 | 唯一 provider 为 OpenRouter；主模型、辅助模型及唯一白名单均为指定免费模型，无付费回退 |
| CLI 事件 | 20 条事件，6 条 `step_finish` 的 cost 均为 0，最终 reason 为 `stop`；退出码 0，无错误事件 |
| 模型实现 | `greeting()` 对输入调用 `trim()`，空白名称回退到 `world`，保留普通名称行为 |
| 模型回归测试 | 新增带空白名称与纯空白名称测试，包含制表符和换行；副本共 4 项测试全部通过 |
| 独立行为验收 | 带空白名称、纯空白名称和缺省名称的独立断言通过，退出码 0；空输出文件表示无断言错误 |
| 前后快照 | 从 `e378190b…dcb19` 变为 `2068155b…6bd7b`；实现和测试均变化，原始源仓库摘要保持不变 |
| 最终结果 | `PASSED_REAL_MODEL_TASK`，验证脚本退出码 0 |
| 清理 | `runtime_stopped=true`；另行复核无托管容器、网络和运行状态文件，未创建模型认证文件 |

完整 [run-06 证据索引](evidence/m1-openrouter-20261024/README.md#run-06真实模型任务通过) 包含脱敏事件、实际任务、前后快照、模型代码差异、测试与验收输出、原始报告、运行配置摘要、API 消息摘要、版本、运行条件、清理结果及 SHA-256 清单。API 摘要是执行中的观察，不冒充最终 CLI 事件；报告内 `capabilities.model_execution=NOT_ATTEMPTED` 是提交前探测，实际任务状态看顶层 `submission_attempted` 和最终结果。

31 项 Python 回归与仓库原始 2 项 Node.js 测试通过。4 项模型测试发生在独立副本；仓库的 `poc/sample-project` 保留原始基线，便于下次验证。没有人工修改样例来冒充模型产出。此次通过仅覆盖内置小任务，不代表完整工作台、业务项目接入或活动任务恢复已验收。

## 6. 仍待完成

1. 活动模型任务的中止、事件流及控制进程断线恢复。
2. 用户业务项目的语言运行时、服务依赖和本机环境一致性。
3. 数据库、缓存及队列的跨需求可写状态隔离。
4. 完整流程状态机、多模型评审及材料/测试反馈闭环。

前三项属于后续 M1 验证及用户项目接入；完整流程按实施计划进入后续迭代。
