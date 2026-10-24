# M2 本地工作台操作说明

- 版本：0.2.0
- 日期：2026-10-24
- 范围：单人、单机、每个需求关联一个项目；模型适配复用 M1

## 1. 已实现的使用流程

登录 → 登记 Git 项目 → 创建需求 → 创建独立工作区 → 启动环境 → 提交 Agent 任务 → 查看事件和差异 → 人工通过/退回。

需求可以分别创建和保存，停止容器保留代码与 Agent 主目录。自审绑定当前代码摘要；Runner 空闲时检查已通过工作区，发现变化后标记为需要重新确认。

## 2. 进程和目录

| 层次 | 实现 |
| --- | --- |
| 界面 | `web/`，React 19、TypeScript 7、Vite 8；由本机 API 提供构建产物 |
| 控制 API | `api.py`、`store.py`，FastAPI、SQLAlchemy；不调用 Docker |
| 执行器 | `runner.py`、`execution.py`，独立进程，通过认证接口领取类型化操作 |
| 数据库 | 默认 SQLite WAL；可选 PostgreSQL；Alembic 迁移 `0001 → 0002` |
| Agent | M1 Docker 镜像；模型、辅助模型和 provider 白名单固定为所选免费模型 |

默认持久目录 `.local-data/workbench/`：

```text
access-token        # 浏览器登录，私有文件
runner-token        # 执行器认证，私有文件
workbench.db        # SQLite 元数据、队列、事件和确认
locks/              # 单机需求写锁
workspaces/         # 需求代码、Agent 主目录及私有运行状态
```

API 和 Runner 必须使用同一个绝对数据目录。Agent 容器不挂载访问令牌、控制数据库或其他需求目录。两个令牌分别生成，不是模型服务凭据。

## 3. 启动

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
npm ci --prefix web
npm run --prefix web build
docker build -t agent-workbench-m1:local poc/container
.venv/bin/python -m agent_workbench.server --data-dir /absolute/path/workbench-data
```

另一终端：

```bash
.venv/bin/python -m agent_workbench.runner --data-dir /absolute/path/workbench-data
```

API 默认仅监听 `127.0.0.1:8340`。打开本机地址，读取启动提示中的 `access-token` 私有文件，在登录表单输入令牌。Cookie 为 HttpOnly、SameSite Strict；写请求检查来源，内部 Runner 接口使用独立凭据。

启动会应用数据库迁移，既有需求及执行记录保留。手动初始化可加 `--init-only`。后台代码不会自动重放旧执行。

构建和运行需要代理证书时，按 M1 操作说明传入 BuildKit secret。Runner 检测到 `CODEX_PROXY_CERT` 时将证书只读挂载到容器，继承已有代理，不关闭 TLS 检查。

## 4. 模型配置

默认沿用 M1 验证的模型：

```text
openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
```

Runner 读取已经配置好的 `OPENROUTER_API_KEY` 环境变量，API、网页和数据库不接收明文模型密钥字段。新启动环境按 M1 方式注入，执行前通过公开目录检查所选模型所有价格项为有限数值 0。没有现有凭据时会明确失败，不创建凭据、不切换付费服务。

API 与 Runner 的 `AWB_MODEL` 必须一致。可以显式切换到 OpenCode 免费模型，例如：

```bash
AWB_MODEL=opencode/glm-5.3-flash-free .venv/bin/python -m agent_workbench.server
AWB_MODEL=opencode/glm-5.3-flash-free .venv/bin/python -m agent_workbench.runner
```

两条命令在各自终端执行。免费额度由服务商决定，模型列出不代表推理可用。切换模型后停止并重新启动需求环境，以应用新配置；不静默沿用旧 profile。

本机访问令牌、Runner 令牌、模型密钥及 Agent 认证数据均应留在被忽略的数据目录或进程环境中。

## 5. PostgreSQL

```bash
.venv/bin/python -m pip install -e '.[server,postgres]'
```

将 `AWB_DATABASE_URL` 配置为 PostgreSQL 的 SQLAlchemy URL（`postgresql+psycopg://...`），再启动 API。数据库密码通过本机私有配置提供，不填写进仓库说明或提交 Git。

Runner 不直接连接数据库。单机文件锁仍是当前执行边界，因此支持 PostgreSQL 不等于已经支持多机 Runner。

## 6. 任务和恢复规则

- 同一需求仅允许一个排队/执行任务；不同需求互相独立。
- 相同 Idempotency-Key 和相同输入返回原任务；同一 key 对应不同输入会被拒绝。
- 任务领取、心跳、事件和完成结果均检查 owner、epoch 及租约。
- 心跳过期进入 UNKNOWN，禁止普通后续操作；不会自动提交第二次 Agent 任务。
- 页面“停止并核实”取得同一需求文件锁、核实运行资源停止后，解除旧 UNKNOWN 阻塞。
- 取消运行任务由 Runner 停止并核实容器，保留已有代码。停止未确认时继续阻塞。
- 控制回调中断或 Agent 没有明确结束事件时，不登记为正常完成。
- 模型会话完成不代表业务验收或测试通过；最终仍查看代码、日志并人工确认。

多个单机 Runner 可以领取不同需求任务，容器资源按 M1 限制。总资源准入和多机调度属于后续迭代。

## 7. 自审和显示限制

自审请求包含代码 tree_digest；Runner 重新计算后才保存决定。摘要变化时旧决定保留为历史，当前状态改为 STALE。退回需要说明原因。

界面展示最近 500 条事件，单条输出最多展示 16,000 字符。差异展示最多 500,000 字符，单个新增文件最多 100,000 字符；任一限制导致截断都会明确提示并阻止在当前界面通过自审，不把不完整显示当作审查完成。

列表不重复返回全部代码差异；详情不重复返回每次任务的大型产物。持久化事件可按游标回放。

## 8. 当前接口

| 接口 | 用途 |
| --- | --- |
| `/api/v1/session` | 登录、退出、会话状态 |
| `/api/v1/projects` | 项目登记及列表 |
| `/api/v1/requirements` | 需求创建及列表 |
| `/api/v1/requirements/{id}` | 需求、工作区、执行和自审历史 |
| `/api/v1/requirements/{id}/actions` | prepare/start/stop/refresh/task/reconcile |
| `/api/v1/requirements/{id}/reviews` | 快照绑定的人工确认 |
| `/api/v1/jobs/{id}/cancel` | 请求取消 |
| `/api/v1/jobs/{id}/events`、`/stream` | 历史及 SSE 回放 |
| `/internal/*` | Runner 领取、心跳、输出、观察和完成；独立认证 |

## 9. 验证和后续

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test poc/sample-project/server.test.mjs
npm run --prefix web build
```

浏览器检查需安装可选 `.[browser]` 及 Chromium。在专用空数据目录运行 API 和 Runner 后：

```bash
.venv/bin/python poc/verify_workbench.py --data-dir /path/to/qa-data
# 重启 API 和 Runner 后检查保留情况：
.venv/bin/python poc/verify_workbench.py --data-dir /path/to/qa-data --check-existing
# 仅在该验收数据库注入明确标记的 UNKNOWN 测试记录，验证界面恢复：
.venv/bin/python poc/verify_workbench.py --data-dir /path/to/qa-data --check-recovery
```

不需要模型凭据的基础设施验收可在两个进程中显式选择 OpenCode 免费 profile；该验收不会提交推理。PostgreSQL 独立测试使用 `poc/verify_postgres.py --root <new-dir>`，需要可选 PostgreSQL 依赖及 Docker，测试容器结束即删除。

完整流程模板、需求版本管理、范围确认、多模型评审、Supervisor 评审、设计和测试材料仍按 M3/M4 推进。业务项目镜像、数据库等依赖和多项目联调需要后续试点接入。
