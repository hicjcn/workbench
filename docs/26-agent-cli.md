# OpenCode 兼容封装 CLI 配置管理

本次接入范围是用户说明的「基于 OpenCode 封装的 CLI」。新增独立「Agent CLI 配置」页面，支持在内置 OpenCode 与兼容封装之间选择、维护不可变版本、设置新工作区默认值，并在项目运行配置中选择具体版本。SQLite 继续保存全部配置和绑定，不增加数据库服务。

## 页面使用

1. 在「Docker 镜像」准备已安装封装程序的镜像。CLI 管理只保存配置，不安装程序，也不会直接在宿主机执行命令。
2. 打开「Agent CLI 配置」→「添加封装 CLI」，填写名称、容器中的程序名或路径。页面会填充默认的 `serve`、`run`、`--version` 和 `models` 命令；封装更改了参数时可分别编辑。
3. 设置任务超时和可选的精确版本输出。凭据只填写变量名，由 Runner 的既有进程环境按名称传入容器；不在网页、命令参数、SQLite 或备份中保存实际密钥值。
4. 保存后可「设为新工作区默认」。项目也可在「项目运行配置」选择特定 CLI 配置版本，覆盖全局默认。
5. 创建新需求工作区，在「容器管理」执行环境预检，再启动并验证实际任务。需求和容器界面显示所选配置及其是否已经固定。

配置在**创建工作区任务入队时**固定；之后修改默认值或创建配置新版不会改变已绑定的需求。旧工作区继续沿用内置 OpenCode，不根据新的全局默认重新解释。已绑定旧版本的项目/工作区仍能读取历史版本；不通过修改记录来升级现有工作区。

## 命令示例

封装提供兼容的服务与任务子命令时：

```text
服务：team-code serve --hostname {host} --port {port}
任务：team-code run --attach {endpoint} --model {model} --format json
版本：team-code --version
模型列表：team-code models
```

如果封装只处理任务，可将服务命令保留为原生 OpenCode：

```text
服务：opencode serve --hostname {host} --port {port}
任务：team-code execute --server {endpoint} --model {model} --format json
版本：team-code --version
```

第二种仅是参数布局示意，实际子命令以封装实现为准。用户已确认实际封装仅可执行程序名不同，其余 OpenCode 子命令与 JSON 格式一致。因此通常只需填写程序名，由页面生成命令；本轮仍使用示例封装验证，用户的实际程序镜像尚未单独验收。

支持替换 `{host}`、`{port}`、`{endpoint}`、`{model}`。实际服务监听容器 `0.0.0.0:4096`，任务连接容器内 `http://127.0.0.1:4096`；宿主只发布回环随机端口。提示词固定从标准输入写入，不插入 argv，不支持 `{prompt}`。命令解析为参数数组后通过 `docker exec` 执行，不执行宿主 Shell、管道、重定向或字符串环境插值；复杂封装应使用镜像内的脚本。

凭据/封装变量允许 API_KEY、TOKEN、BASE_URL 后缀或 AGENT_ 前缀，拒绝 AWB_、DOCKER_、LD_、PYTHON、OPENCODE_SERVER_ 等工作台、容器和系统控制变量。配置只记录名称；容器接收现有变量时 argv 使用名称，输出按实际变量值脱敏。

## 兼容契约

- 服务保留 OpenCode 的回环控制 API、健康/配置/技能探测、创建/中止/删除会话能力，并使用 `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` 认证。
- 继续从工作区的固定 OpenCode 配置读取模型/provider 白名单、插件及只读角色权限。新流程轮次可从 CLI 列表选择并固定模型；历史轮次保留原有规则，详见[CLI 模型选择](35-cli-model-selection.md)。
- 任务命令从标准输入读取提示词，输出 OpenCode JSONL 事件：正文为 `type=text` / `part.text`，正常完成含 `type=step_finish` / `part.reason=stop`。错误事件或非零退出都失败；只有退出码 0 没有结束事件则保持待核实，不自动重试。
- 只读分析、模型评审、文档生成和联合分析继承原工作区 CLI 配置，使用独立 Agent HOME 和只读源码挂载，不连接开发数据库。预检、单项目分析、模型评审和文档生成继承固定运行配置中的自定义卷，并遵循每个卷的 `read_only` 设置，允许配置为读写的 CLI 日志、缓存或登录状态目录正常写入。联合分析使用第一个候选项目的 CLI/镜像，不继承项目配置挂载。
- 预检使用配置中的版本命令；预期版本为空时只要求命令成功且有版本输出。固定镜像、Node/Git、命令入口、配置只读挂载、Superpowers 技能发现及控制接口检查仍然保留；Superpowers 不再校验固定版本或提交标签。

预检不要求镜像中额外安装名为 `opencode` 的程序：所选封装 CLI 的服务与版本命令可直接使用自定义程序名。版本输出支持 stdout 或 stderr。启动容器时保留镜像自身的 `ENTRYPOINT` 和 `CMD`，例如只启动 code-server；容器启动后，工作台通过 `docker exec` 启动当前需求固定的 CLI 服务，再等待兼容控制接口就绪。Dockerfile 不需要启动 Agent CLI，也不应预先占用 `4096/tcp`。

预检页显示实际固定 CLI、服务/版本命令和检测到的版本；Agent 服务单独记录状态、退出码与脱敏日志。code-server 仍在运行时，CLI 退出也会明确报错；容器退出则单独诊断。旧版内置 `agent-workbench-m1:local` 默认启动 OpenCode，升级后需重新构建该镜像及依赖它的项目镜像，再停止已有环境、同步最新配置并重新预检，避免重复启动服务。详见[封装 CLI 预检](32-wrapper-cli-preflight.md)。

如果封装改变 HTTP 接口、认证、标准输入或事件格式，需要增加桥接脚本/协议适配，不能仅更换可执行文件名。预检不调用模型，不证明封装业务逻辑或模型推理已通过。建议把封装程序安装到镜像只读目录，避免由待开发源码决定执行程序。

实际验证发现 OpenCode 1.19.4 会自动在配置写入官方 `$schema`。比较时仅兼容值精确为 `https://opencode.ai/config.json` 的该元数据；模型、provider、权限及额外字段仍要求完全一致。

## 持久化与恢复

新增迁移 `0008`：`agent_cli_configs` 保存不可变版本；`agent_cli_default` 保存默认选择；`workspace_agents` 保存需求绑定。工作区清单同时保存经摘要校验的配置，开始模型操作前核对数据库绑定，防止副本或旧备份中的配置静默覆盖当前选择。

离线备份 v2/0008 包含配置、默认值和工作区绑定，认证值不包括在内。兼容恢复既有 v1/0005、v1/0006、v2/0006、v2/0007 备份，旧目录默认保持 OpenCode；恢复不执行任何 CLI 任务。CLI 程序所在镜像仍需单独准备。

## 验证范围

新增 13 项单元/API 检查，覆盖配置版本、权限、命令/变量验证、固定时机、项目覆盖、历史兼容、任务命令选择、凭据脱敏、官方 schema 兼容及备份恢复。实际容器检查用 [示例封装](../poc/cli-wrapper/rootfs/opt/awb-opencode.mjs) 委托真实 OpenCode 服务，验证配置的服务/版本命令、认证接口与技能、默认切换后的固定配置、只读隔离、浏览器停止及 SQLite 备份恢复；任务部分使用明确标记的受控本地 JSON fixture，不是在线推理。用户自己的封装 CLI 仍需提供并验证实际命令。

Sites 试用同步管理页面、项目选择、版本/default 切换及导出，配置保存在浏览器；不安装工具、不执行真实 CLI。

复现：

```bash
.venv/bin/python -m unittest discover -s tests -v
npm run --prefix web build
.venv/bin/python poc/verify_agent_cli.py --root .local-data/agent-cli-new
```

结果见 [容器与传输检查](evidence/agent-cli-report.json) 和 [站点管理检查](evidence/agent-cli-site-report.json)。本轮未执行真实模型推理，不替代 M10 在线全流程和业务验收。
