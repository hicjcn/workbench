# 日志模式与执行进度

在现有 `workbench.toml` 的 `[workbench]` 下配置：

```toml
[workbench]
log_mode = "concise" # 默认精简；改为 "detailed" 开启详细日志
```

没有此字段的旧配置默认使用 `concise`。只接受 `concise` 和 `detailed`，其他值会报配置错误。环境变量 `AWB_LOG_MODE` 优先于文件，启动器会把最终值传给 API 和 Runner。修改后停止再启动实例；`status.cmd` 的 `log_mode` 显示当前实例实际模式。已有任务的历史日志不随模式切换补写或删除。

| 内容 | 精简 `concise` | 详细 `detailed` |
| --- | --- | --- |
| 启动、数据库迁移、服务就绪、停止与资源核实 | 显示 | 显示 |
| 任务领取、源码复制、等待 CLI 就绪、工具预检、结果登记 | 显示关键步骤 | 显示关键步骤及内部步骤 |
| 逐仓库复制、固定运行配置、Git 导出、单项预检 | 省略内部进度 | 显示 |
| Docker 操作类型、退出码与耗时 | 由关键步骤及失败结果说明 | 显示每次调用的进度和耗时 |
| HTTP 请求状态和耗时 | 显示失败请求，正常轮询保持安静 | 显示路由模板、方法、状态、关联 ID 和耗时 |
| 执行超过 15 秒的任务与镜像操作 | 保留开始阶段，直到结果返回 | 每 15 秒显示仍在执行及租约状态 |
| 失败阶段和任务最终状态 | 显示 | 另附代码位置，不输出异常中的私有数据 |
| 页面已有的 Agent 回复、验证输出和失败报告 | 保留 | 保留，新增详细阶段标记为“详细” |

## 文件和页面位置

通过本地启停脚本启动时，日志写到：

- `<logs_dir>/launcher.log`：实例管理、API/Runner 启动顺序、就绪和停止核实。
- `<logs_dir>/<instance>/api.log`：数据库升级、路由初始化、操作接收和 HTTP 状态。
- `<logs_dir>/<instance>/runner.log`：任务领取、当前执行阶段、Docker 操作及结果写回。

`start.cmd` / `status.cmd` 返回的 `logs_dir` 是当前实例目录。直接运行 Python API 或 Runner 时，同样的日志输出到 stderr，可用终端查看或自行重定向。启动器、API 和 Runner 的日志统一使用带毫秒的 UTC 时间（`Z` 后缀）、级别、组件和 `step`；任务记录还带 `job_id`、`requirement_id`，可用同一 ID 串联不同文件中的执行过程。耗时字段单位为毫秒。

页面展开需求的“执行记录”，选择一次操作查看阶段；正在运行时，日志上方显示“当前步骤”。精简模式也能看到源码准备、CLI 启动/等待、工具检查、结果收集等关键进度。详细模式为新执行增加内部阶段事件；已有 Agent 输出和业务验证记录不受此模式过滤。

示意格式（ID 与时间仅用于说明）：

```text
2026-10-27T10:00:00.100Z INFO [agent_workbench.runner] 正在创建独立源码副本 step="workspace.copy" job_id="job_example" requirement_id="req_example" action="prepare"
2026-10-27T10:00:00.125Z DEBUG [agent_workbench.workspace] 正在导出固定 Git 提交 step="source.archive" job_id="job_example" requirement_id="req_example" action="prepare"
```

完整命令、模型正文和业务文件内容不写入进程进度日志。HTTP 只记录路由模板，不记录请求体、查询参数、认证头、Cookie 或未知 URL 中的文本。进程日志中的已配置凭据会脱敏；构建输出继续按原机制丢弃。详细模式不会扩大 Agent 权限，也不会改变任务重试、取消或资源清理规则。

## Windows 11 使用示例

修改配置文件后重启：

```powershell
.\stop.cmd
.\start.cmd
.\status.cmd
```

临时覆盖文件中的日志模式：

```powershell
$env:AWB_LOG_MODE = "detailed"
.\stop.cmd
.\start.cmd
# 之后希望恢复读取配置文件时，先移除当前终端的覆盖值：
Remove-Item Env:AWB_LOG_MODE
```

查看启动器日志，或把下面的实例目录替换为 `status.cmd` 返回的 `logs_dir`：

```powershell
Get-Content .local-data\logs\launcher.log -Tail 100 -Wait
Get-Content '.local-data\logs\<instance>\runner.log' -Tail 100 -Wait
```

如任务没有继续输出，先按 `job_id` 找到最后一个步骤，再查看任务最终状态及同实例的 API/Runner 日志。详细模式中任务持续续期表示 Runner 仍在执行；租约丢失或 UNKNOWN 按原流程停止并核实，日志模式不会自动重放任务。

## 验证

`tests/test_logging_support.py` 覆盖模式过滤、请求摘要、事件连续性、上下文隔离和脱敏；`tests/test_launcher.py` 用真实 API/Runner 验证两种模式下的准备任务、启停及脚本 JSON 输出。完整回归命令为 `python poc/run_unit_tests.py`。

真实 Docker 回归使用 `poc/verify_preflight_cli.py` 的受控兼容 CLI；页面的当前步骤、详细标记和历史回放使用受控任务事件验证，不执行模型推理。结果见[日志与进度验收记录](evidence/progress-logging-report.json)。
