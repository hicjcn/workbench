import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, timeLabel } from "./api";
import type { Project } from "./api";
import { Modal } from "./Forms";
import type { AgentCliState } from "./AgentCliPanel";
import { RuntimeImageSelect } from "./RuntimeImageSelect";
import { RuntimeConfigFields } from "./RuntimeConfigFields";
import type {
  EnvironmentEntry,
  PortMapping,
  PortRange,
  VolumeMount,
} from "./RuntimeConfigFields";

export type RuntimeProfile = {
  id: string;
  number: number;
  content_digest: string;
  actor: string;
  reason: string;
  created_at: number;
  content: {
    agent_cli_id?: string | null;
    image: string;
    cpus: number;
    memory_mib: number;
    pids_limit: number;
    build_command: string;
    verification_command: string;
    repository_commands?: {
      path: string;
      build_command: string;
      verification_command: string;
    }[];
    environment?: Record<string, string>;
    port_mappings?: PortMapping[];
    volume_mounts?: VolumeMount[];
    mounts: { source: string; target: string; sha256?: string | null }[];
    dependencies?: {
      kind: "postgres";
      image: string;
      cpus: number;
      memory_mib: number;
      pids_limit: number;
    }[];
    application?: {
      start_command: string;
      port: number;
      health_path: string;
      expected_status: number;
      ready_timeout_seconds: number;
    } | null;
  };
};
type ProfileState = {
  port_range?: PortRange;
  current: RuntimeProfile | null;
  history: RuntimeProfile[];
};

export function ProjectProfilePanel({ project }: { project: Project }) {
  const [state, setState] = useState<ProfileState | null>(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const endpoint = `/projects/${project.id}/profiles`;
  useEffect(() => {
    let live = true;
    void api<ProfileState>(endpoint)
      .then((value) => {
        if (live) setState(value);
      })
      .catch((failure) => {
        if (live) setError(failure.message);
      });
    return () => {
      live = false;
    };
  }, [endpoint]);
  async function save(value: Record<string, unknown>) {
    await api(endpoint, value);
    setState(await api<ProfileState>(endpoint));
    setEditing(false);
  }
  return (
    <div className="project-profile">
      <h3>项目运行配置</h3>
      {state?.current ? (
        <>
          <p>
            v{state.current.number} · {state.current.content.cpus} CPU ·{" "}
            {state.current.content.memory_mib} MiB
          </p>
          <code className="path">{state.current.content.image}</code>
          <p className="hint">
            验证：
            {state.current.content.repository_commands?.length
              ? `${state.current.content.repository_commands.length} 个仓库分别配置`
              : state.current.content.verification_command}
          </p>
          <p className="hint">
            {Object.keys(state.current.content.environment ?? {}).length}{" "}
            个环境变量 · {state.current.content.port_mappings?.length ?? 0}{" "}
            个端口映射 ·{" "}
            {state.current.content.mounts.length +
              (state.current.content.volume_mounts?.length ?? 0)}{" "}
            个挂载 · {state.current.actor}
          </p>
          <details>
            <summary>配置历史（{state.history.length}）</summary>
            {state.history.map((item) => (
              <p className="hint" key={item.id}>
                v{item.number} · {timeLabel(item.created_at)} · {item.actor} ·{" "}
                {item.reason}
              </p>
            ))}
          </details>
        </>
      ) : (
        <p className="hint">
          保存镜像、资源、命令、环境变量、端口和挂载，新工作区会固定使用该版本。
        </p>
      )}
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      <button
        className="secondary"
        disabled={!state}
        onClick={() => setEditing(true)}
      >
        {state?.current ? "修订运行配置" : "保存运行配置"}
      </button>
      {editing && (
        <Modal wide title="项目运行配置" close={() => setEditing(false)}>
          <ProfileForm
            current={state?.current ?? null}
            portRange={state?.port_range}
            image={project.image}
            repositories={project.repositories ?? []}
            submit={save}
          />
        </Modal>
      )}
    </div>
  );
}

function ProfileForm({
  current,
  portRange,
  image,
  repositories,
  submit,
}: {
  current: RuntimeProfile | null;
  portRange?: PortRange;
  image: string;
  repositories: string[];
  submit: (value: Record<string, unknown>) => Promise<void>;
}) {
  const [mounts, setMounts] = useState(
    current?.content.mounts.map(({ source, target }) => ({ source, target })) ??
      [],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const defaults = current?.content;
  const [environment, setEnvironment] = useState<EnvironmentEntry[]>(() =>
    Object.entries(defaults?.environment ?? {}).map(([name, value]) => ({
      name,
      value,
    })),
  );
  const [ports, setPorts] = useState<PortMapping[]>(
    defaults?.port_mappings ?? [],
  );
  const [volumes, setVolumes] = useState<VolumeMount[]>(
    defaults?.volume_mounts ?? [],
  );
  const [repositoryCommands, setRepositoryCommands] = useState(() =>
    repositories.map(
      (path) =>
        defaults?.repository_commands?.find((item) => item.path === path) ?? {
          path,
          build_command: "",
          verification_command: "",
        },
    ),
  );
  const [agents, setAgents] = useState<AgentCliState | null>(null);
  useEffect(() => {
    let live = true;
    void api<AgentCliState>("/agent-clis")
      .then((value) => {
        if (live) setAgents(value);
      })
      .catch((failure) => {
        if (live) setError(failure.message);
      });
    return () => {
      live = false;
    };
  }, []);
  const [postgres, setPostgres] = useState(
    Boolean(current?.content.dependencies?.length),
  );
  const [application, setApplication] = useState(
    Boolean(defaults?.application),
  );
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    if (!agents) {
      setError("请先等待 Agent CLI 配置列表加载，失败时关闭表单后重试");
      return;
    }
    if (
      new Set(environment.map((item) => item.name)).size !== environment.length
    ) {
      setError("环境变量名不能重复");
      return;
    }
    if (ports.some((item) => item.service === "application") && !application) {
      setError("配置应用服务端口映射前，请先启用应用服务");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await submit({
        expected_profile: current?.id ?? null,
        actor: data.get("actor"),
        reason: data.get("reason"),
        content: {
          agent_cli_id: data.get("agent_cli") || null,
          image: data.get("image"),
          cpus: Number(data.get("cpus")),
          memory_mib: Number(data.get("memory")),
          pids_limit: Number(data.get("pids")),
          build_command: data.get("build"),
          verification_command: data.get("verify"),
          repository_commands: repositoryCommands.filter(
            (item) =>
              item.build_command.trim() || item.verification_command.trim(),
          ),
          mounts,
          environment: Object.fromEntries(
            environment.map((item) => [item.name, item.value]),
          ),
          port_mappings: ports,
          volume_mounts: volumes,
          application: application
            ? {
                start_command: data.get("app_command"),
                port: Number(data.get("app_port")),
                health_path: data.get("app_health"),
                expected_status: Number(data.get("app_status")),
                ready_timeout_seconds: Number(data.get("app_timeout")),
              }
            : null,
          dependencies: postgres
            ? current?.content.dependencies?.length
              ? current.content.dependencies
              : [
                  {
                    kind: "postgres",
                    image: "postgres:17-bookworm",
                    cpus: 0.5,
                    memory_mib: 512,
                    pids_limit: 128,
                  },
                ]
            : [],
        },
      });
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="form-stack profile-form" onSubmit={save}>
      <p className="hint">保存后创建新的运行配置版本，只影响后续新工作区。</p>
      <div className="profile-config-grid">
        <fieldset className="profile-section">
          <legend>基础环境与资源</legend>
          <RuntimeImageSelect initialImage={defaults?.image ?? image} />
          <label>
            Agent CLI 配置
            <select
              aria-label="Agent CLI 配置"
              name="agent_cli"
              defaultValue={defaults?.agent_cli_id ?? ""}
              disabled={!agents}
            >
              <option value="">跟随系统默认（准备工作区时固定）</option>
              {agents?.configs.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.name} · v{item.number}
                </option>
              ))}
            </select>
          </label>
          <p className="hint">
            选择兼容封装 CLI
            时，镜像必须已经包含相应程序。只影响后续新工作区，已有需求继续使用其固定版本。
          </p>
          <div className="profile-resources">
            <label>
              CPU 核数
              <input
                type="number"
                name="cpus"
                min={0.25}
                max={16}
                step={0.25}
                defaultValue={defaults?.cpus ?? 1}
                required
              />
            </label>
            <label>
              内存 MiB
              <input
                type="number"
                name="memory"
                min={256}
                max={32768}
                defaultValue={defaults?.memory_mib ?? 1536}
                required
              />
            </label>
            <label>
              进程上限
              <input
                type="number"
                name="pids"
                min={64}
                max={2048}
                defaultValue={defaults?.pids_limit ?? 256}
                required
              />
            </label>
          </div>
        </fieldset>
        <fieldset className="profile-section">
          <legend>构建与验证</legend>
          <label>
            构建命令（可留空）
            <input
              name="build"
              defaultValue={defaults?.build_command ?? ""}
              maxLength={2000}
              placeholder="例如：./scripts/build"
            />
          </label>
          <label>
            验证命令
            <input
              name="verify"
              required={!repositories.length}
              defaultValue={
                defaults?.verification_command ??
                (repositories.length ? "" : "node --test")
              }
              maxLength={2000}
            />
          </label>
          <p className="hint">
            命令在需求容器的源码根目录执行；多仓库项目为共同父目录，可提供统一验证脚本。配置逐仓库验证后，统一验证命令可留空。复杂步骤写入仓库脚本；不支持管道、重定向或多条命令拼接。
          </p>
          {Boolean(repositories.length) && (
            <fieldset className="repository-options">
              <legend>逐仓库构建与验证</legend>
              {repositoryCommands.map((item, index) => (
                <div key={item.path}>
                  <strong>
                    <code>{item.path}</code>
                  </strong>
                  <label>
                    构建命令（可选）
                    <input
                      aria-label={`${item.path} 构建命令`}
                      value={item.build_command}
                      maxLength={2000}
                      onChange={(event) =>
                        setRepositoryCommands((previous) =>
                          previous.map((value, position) =>
                            position === index
                              ? { ...value, build_command: event.target.value }
                              : value,
                          ),
                        )
                      }
                      placeholder="例如 mvn -DskipTests package"
                    />
                  </label>
                  <label>
                    验证命令
                    <input
                      aria-label={`${item.path} 验证命令`}
                      value={item.verification_command}
                      maxLength={2000}
                      onChange={(event) =>
                        setRepositoryCommands((previous) =>
                          previous.map((value, position) =>
                            position === index
                              ? {
                                  ...value,
                                  verification_command: event.target.value,
                                }
                              : value,
                          ),
                        )
                      }
                      placeholder="例如 mvn test"
                    />
                  </label>
                </div>
              ))}
              <p className="hint">
                分别在对应仓库目录执行。选择逐仓库验证的需求必须为其全部候选仓库配置验证命令；任一失败都会阻止进入评审。
              </p>
            </fieldset>
          )}
        </fieldset>
        <RuntimeConfigFields
          environment={environment}
          setEnvironment={setEnvironment}
          ports={ports}
          setPorts={setPorts}
          volumes={volumes}
          setVolumes={setVolumes}
          portRange={portRange}
          application={application}
        />
        <fieldset className="profile-section">
          <legend>只读配置文件副本</legend>
          <p className="hint">
            只登记本机普通文件的绝对路径，不填写密钥内容。准备工作区时复制并固定摘要，内容不会进入工作台备份。
          </p>
          {mounts.map((item, index) => (
            <div className="delivery-form-member" key={index}>
              <label>
                配置源文件
                <input
                  value={item.source}
                  required
                  onChange={(event) =>
                    setMounts((previous) =>
                      previous.map((value, position) =>
                        position === index
                          ? { ...value, source: event.target.value }
                          : value,
                      ),
                    )
                  }
                />
              </label>
              <label>
                容器配置路径
                <input
                  value={item.target}
                  required
                  placeholder="/run/workbench-config/settings.conf"
                  onChange={(event) =>
                    setMounts((previous) =>
                      previous.map((value, position) =>
                        position === index
                          ? { ...value, target: event.target.value }
                          : value,
                      ),
                    )
                  }
                />
              </label>
              <button
                className="text-button"
                type="button"
                onClick={() =>
                  setMounts((previous) =>
                    previous.filter((_, position) => position !== index),
                  )
                }
              >
                移除配置文件
              </button>
            </div>
          ))}
          <button
            className="secondary"
            type="button"
            disabled={mounts.length >= 8}
            onClick={() =>
              setMounts((previous) => [
                ...previous,
                { source: "", target: "/run/workbench-config/" },
              ])
            }
          >
            添加配置文件
          </button>
        </fieldset>
        <fieldset className="profile-section">
          <legend>业务项目的可选依赖</legend>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={postgres}
              onChange={(event) => setPostgres(event.target.checked)}
            />
            业务项目需要时，为每个需求启用 PostgreSQL 17
          </label>
          <p className="hint">
            默认不启用。工作台默认使用本地
            SQLite，无需数据库服务；只有待开发的业务项目需要 PostgreSQL
            时才勾选。
            每个需求独立数据库、内部网络、数据卷和认证，不开放宿主端口。依赖占用一个容器名额；启动前需备好
            postgres:17-bookworm 镜像。停止会先保存快照并保留数据卷。
          </p>
        </fieldset>
        <fieldset className="profile-section">
          <legend>应用服务</legend>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={application}
              onChange={(event) => setApplication(event.target.checked)}
            />
            启用应用服务与 HTTP 检查
          </label>
          <p className="hint">
            应用另占一个容器名额，使用固定镜像和只读代码；连接本需求数据库。通过
            PORT 环境变量设置端口，检查在容器内执行。
          </p>
          {application && (
            <>
              <label>
                服务启动命令
                <input
                  name="app_command"
                  required
                  maxLength={2000}
                  defaultValue={
                    defaults?.application?.start_command ?? "node server.cjs"
                  }
                />
              </label>
              <label>
                服务端口
                <input
                  name="app_port"
                  type="number"
                  required
                  min={1024}
                  max={65535}
                  defaultValue={defaults?.application?.port ?? 3000}
                />
              </label>
              <label>
                HTTP 检查路径
                <input
                  name="app_health"
                  required
                  maxLength={500}
                  defaultValue={defaults?.application?.health_path ?? "/health"}
                />
              </label>
              <label>
                预期 HTTP 状态
                <input
                  name="app_status"
                  type="number"
                  required
                  min={200}
                  max={299}
                  defaultValue={defaults?.application?.expected_status ?? 200}
                />
              </label>
              <label>
                就绪等待秒数
                <input
                  name="app_timeout"
                  type="number"
                  required
                  min={1}
                  max={60}
                  defaultValue={
                    defaults?.application?.ready_timeout_seconds ?? 30
                  }
                />
              </label>
            </>
          )}
        </fieldset>
      </div>
      <div className="profile-form-footer">
        <div className="config-pair profile-audit">
          <label>
            登记人
            <input
              name="actor"
              required
              maxLength={160}
              defaultValue={current?.actor ?? ""}
            />
          </label>
          <label>
            配置原因
            <textarea name="reason" rows={1} required maxLength={4000} />
          </label>
        </div>
        {error && (
          <p role="alert" className="inline-error">
            {error}
          </p>
        )}
        <button className="primary" disabled={busy}>
          {busy ? "保存中…" : "保存新版本"}
        </button>
      </div>
    </form>
  );
}
