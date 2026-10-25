# M6 项目接入与环境预检

本轮按用户选择使用样例完善接入能力，暂不接入私有业务项目或调用在线模型。

项目运行配置保存不可变版本，包含已有镜像、资源限额、构建/验证命令及显式只读配置文件。需求第一次提交创建工作区时固定配置版本，Runner 准备时固定实际镜像 ID，并将允许挂载的配置文件复制到该工作区的私有目录。更新项目配置只影响后续工作区，不静默替换进行中的需求环境。

环境预检使用独立只读副本和独立 Agent 主目录，检查镜像、OpenCode/Superpowers、命令入口和挂载。不调用模型、不执行构建或业务测试。构建与验证另设明确操作，在需求容器内执行；验证命令可作为开发流程的默认值。

Dockerfile 由用户维护并在本机先构建为镜像。当前仍以已锁定的 OpenCode/Superpowers 基础镜像为契约，不在 API 中执行任意宿主构建脚本，不实现 Compose 业务依赖编排或任意可写宿主挂载。

备份包含配置元数据、文件摘要和工作区绑定，不包含挂载文件内容；恢复后需从登记路径提供与固定摘要一致的配置文件。M5 备份需兼容读取，旧工作区保留原有运行行为。

## 先用 Node 样例接入

构建已有的固定工具镜像，再构建本轮新增的示例镜像：

```bash
docker build -t agent-workbench-m1:local poc/container
docker build -t agent-workbench-node:local poc/onboarding
```

代理环境沿用 Docker 的代理设置，使用提供的 CA 挂载，不关闭 TLS 校验；基础镜像命令可加 `--secret id=proxy_ca,src="$CODEX_PROXY_CERT"`。自定义 Dockerfile 可以在固定基础镜像上添加 Java、Maven、Go 等项目工具，但需保留 OpenCode/Superpowers、Node、Git 及原有启动契约。当前没有网页构建镜像或 Compose 依赖编排功能。

为样例准备独立 Git 仓库，目标目录需尚不存在；不要将工作台自身仓库作为样例项目：

```bash
(
  set -e
  sample_project_dir="$HOME/workbench-samples/node-service"
  test ! -e "$sample_project_dir"
  mkdir -p "$HOME/workbench-samples"
  cp -R poc/sample-project "$sample_project_dir"
  git -C "$sample_project_dir" init -b main
  git -C "$sample_project_dir" add --all
  git -C "$sample_project_dir" -c user.name='Workbench Sample' \
    -c user.email='sample@example.invalid' commit -m 'Initial sample'
)
```

1. 更新安装并构建页面：`.venv/bin/python -m pip install -e '.[server]'`、`npm ci --prefix web`、`npm run --prefix web build`。
2. 启动 API 和 Runner，使用相同的数据目录与模型配置。本轮预检不需要模型凭据；正常启动/开发仍使用已选择的模型配置，模型认证单独管理。
3. “项目登记”选择 `$HOME/workbench-samples/node-service` 这个实际绝对路径作为源码目录。配置运行镜像为 `agent-workbench-node:local`，构建命令为 `node --check server.mjs`，验证命令为 `node --test server.test.mjs`；资源默认 1 CPU、1536 MiB、256 个进程即可。
4. 填写登记人和原因，保存运行配置。等价的 API 请求体见 [profile.sample.json](../poc/onboarding/profile.sample.json)，这是本轮可解析的 JSON；旧 `design-examples/*.yaml` 仍是整体设计示意，不是此接口格式。
5. 新建需求，点击“创建工作区”，然后在“项目环境与预检”点击“环境预检”。预检通过后再点击“启动环境”。
6. 点击“执行项目构建”“执行项目验证”，从执行记录查看真实退出码和输出。样例后端包含两个测试用例。这里的通过不自动完成 M3 流程评审、主管审批或 M4 测试登记。
7. 开启开发流程时，“验证命令”默认使用工作区固定配置中的命令，可在开启本轮时明确修改；当前轮次会保存实际命令。

## 配置版本与环境边界

保存配置需要提交页面载入时的 `expected_profile`；并发修改或过期表单会返回冲突。历史版本保留内容、摘要、登记人和原因。第一次提交创建工作区时固定版本，即使排队操作被取消，后续也保留该绑定；更新项目配置只影响尚未固定配置的新工作区。

Runner 首次准备时解析镜像标签为实际 image ID。以后开发、分析、评审、材料及预检副本都使用这个镜像 ID 和同一资源配置。标签后续指向其他镜像时，不会替换进行中需求的环境。旧工作区没有固定配置时沿用 M5 行为；要使用新能力需创建新需求工作区，不自动改写旧环境。

资源范围：CPU 0.25—16、内存 256—32768 MiB、进程数 64—2048。单个配置最多 8 个挂载文件，每个最多 1 MiB。命令以参数数组在容器中执行，不经宿主 shell；不支持管道、重定向或拼接多条命令，复杂步骤放入仓库脚本。构建命令可留空，验证命令必填。

挂载仅支持普通文件，目标限定在 `/run/workbench-config/`，只读。不允许目录、符号链接、工作台数据目录文件、重复/重叠目标或 Docker socket。登记页面只填写源路径与目标路径，不填写密钥内容。

保存版本时记录配置文件的 SHA-256；准备时再次核对，复制到工作区私有的 `runtime-config/`，之后使用独立副本。对主机原文件的后续修改不会改变旧工作区。只读角色有独立副本，配置内容不进入 API 响应和预检日志；用户执行的项目命令仍可能自行输出配置内容，现有脱敏不能识别任意业务秘密。

## 预检记录的含义

预检创建独立只读代码副本、配置副本与 Agent 主目录，启动本机 OpenCode 控制服务，检查：

- 实际镜像与固定 ID 一致，OpenCode 为已验证的 `1.19.4`。
- Superpowers 镜像版本标签为固定提交 `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`，已配置且能列出 `using-superpowers` 技能。
- Node/Git 可用，构建和验证命令的入口程序存在，配置文件可读。
- 源码挂载只读，原工作区与预检副本的代码摘要一致。

这验证的是镜像契约、技能发现和命令入口，没有执行模型请求、项目构建、依赖下载或业务测试，也不能证明模型会遵循技能。报告会明确保存 `model_execution=NOT_ATTEMPTED`、`build=NOT_RUN` 和 `business_tests=NOT_RUN`。镜像内工具若被更换需重新完成兼容性验证，当前不会把未验证版本自动视为兼容。

预检完成后停止其容器并保留副本及证据。无法确认停止时任务和报告都变为 `UNKNOWN`，必须通过“停止并核实”处理，不能继续写工作区。版本或命令检查失败会显示具体未通过项，并阻止该固定配置启动；修正项目配置后创建新工作区验证。

## 备份与恢复

`0006` 迁移增加 `project_profiles` 和 `workspace_profiles`，已有业务表不改写。维护备份保存配置版本、文件摘要、工作区绑定、固定镜像 ID、预检报告及原有代码材料，排除 `runtime-config/` 和 Agent 认证目录。恢复支持 M5 `0005` 与本轮 `0006` 逻辑备份。

恢复后第一次启动会在私有目录缺少配置副本时，从登记的原路径读取与固定摘要一致的配置文件；文件缺失、发生变化或权限不可读时明确失败，不静默替换环境。把正确版本文件放回登记路径后重试；选择新配置时需新建工作区。固定运行镜像也需单独保存与导入（如 `docker image save/load`），镜像层不包含在数据备份中；重新构建出不同 ID 时不能自动替换旧环境。

预检及配置摘要会随完整交付包导出，配置文件内容不会单独作为交付条目。配置、构建和样例测试记录不能替代真实业务评审和性能测试。验证与边界见 [M6 验证报告](15-m6-validation.md)。
