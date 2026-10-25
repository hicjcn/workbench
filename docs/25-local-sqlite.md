# 本地 SQLite 存储

工作台默认使用 Python 自带驱动支持的 SQLite 文件，保存项目、需求、版本、流程、执行事件、材料、镜像模板与维护任务。无需部署数据库服务或配置数据库账号密码。SQLAlchemy/Alembic 仍负责数据访问和已有表结构升级，无新增迁移。

```bash
# 首次自动创建数据库，后续启动保留已有记录
.venv/bin/python -m agent_workbench.server --sqlite --data-dir /absolute/path/workbench-data

# 另一终端：Runner 使用相同数据目录，通过 API 领取任务，不直连数据库
.venv/bin/python -m agent_workbench.runner --data-dir /absolute/path/workbench-data
```

数据库为 `/absolute/path/workbench-data/workbench.db`；未指定目录时为当前工作目录下 `.local-data/workbench/workbench.db`。`--sqlite` 优先于 `AWB_DATABASE_URL`，防止遗留的外部数据库环境变量改变本次选择。仅初始化可以加 `--init-only`。

省略 `--sqlite` 且未配置 URL（或变量为空）时，同样默认使用本地 SQLite；显式配置的 PostgreSQL 兼容能力保留。显式切换到 SQLite 不会自动迁移外部数据库中的数据，也不会删除原数据库。

SQLite 采用现有 WAL、外键约束和短事务配置，适合当前单人、单机的工作台。项目和需求并发继续使用现有任务租约、并发上限与文件锁。控制数据库不挂载给 Agent 容器。

「资源与恢复」显示实际存储类型和本地数据库路径；外部数据库仅显示存储类型，不返回连接字符串、账号或密码。业务项目运行配置里的 PostgreSQL 默认关闭，只在待开发项目实际需要时选择，与工作台 SQLite 存储分别配置。

## 持久化与备份

保留整个数据目录，其中还包含源码副本、会话和材料文件。若以后将控制服务放入容器，应将该目录挂载到持久目录/数据卷；只保留容器可写层不能保证重建后数据存在。

停掉 API 和 Runner 后，沿用现有离线维护命令，显式选择相同 SQLite 数据目录：

```bash
agent-workbench-maintenance stop --sqlite --data-dir /absolute/path/workbench-data
agent-workbench-maintenance backup --sqlite --data-dir /absolute/path/workbench-data
agent-workbench-maintenance restore --archive /path/to/backup.zip --destination /path/to/new-data
```

备份包含数据库各表和被校验的源码/材料；不复制访问令牌、Agent 密钥和数据库密码。不要在服务仍写入时单独复制 `workbench.db`，WAL 中可能尚有未合并的事务。

## Sites 试用边界

现有 Sites 页面是交互试用，数据仍保存在浏览器 localStorage；它没有运行 Python API 或真实 SQLite 文件。页面中明确展示本机版的 SQLite 存储说明，不将浏览器存储当作数据库服务已部署。

## 验证

新增检查覆盖默认/空环境变量选择本地文件、`--sqlite` 覆盖外部 URL、没有外部数据库驱动时实际初始化、API 重启后项目/需求/镜像模板持久化，以及存储信息不暴露连接凭据。初始化和 API 持久化检查不调用 Docker、不连接外部数据库、不调用模型。
