import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, timeLabel } from "./api";
import type { Project } from "./api";
import { Modal } from "./Forms";

export type RuntimeProfile = {
  id: string;
  number: number;
  content_digest: string;
  actor: string;
  reason: string;
  created_at: number;
  content: {
    image: string;
    cpus: number;
    memory_mib: number;
    pids_limit: number;
    build_command: string;
    verification_command: string;
    mounts: { source: string; target: string; sha256?: string | null }[];
    dependencies?: {
      kind: "postgres";
      image: string;
      cpus: number;
      memory_mib: number;
      pids_limit: number;
    }[];
  };
};
type ProfileState = {
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
            验证：{state.current.content.verification_command}
          </p>
          <p className="hint">
            {state.current.content.mounts.length} 个只读配置文件 ·{" "}
            {state.current.actor}
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
          保存镜像、资源、命令与只读挂载，新工作区会固定使用该版本。
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
        <Modal title="项目运行配置" close={() => setEditing(false)}>
          <ProfileForm
            current={state?.current ?? null}
            image={project.image}
            submit={save}
          />
        </Modal>
      )}
    </div>
  );
}

function ProfileForm({
  current,
  image,
  submit,
}: {
  current: RuntimeProfile | null;
  image: string;
  submit: (value: Record<string, unknown>) => Promise<void>;
}) {
  const [mounts, setMounts] = useState(
    current?.content.mounts.map(({ source, target }) => ({ source, target })) ??
      [],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const defaults = current?.content;
  const [postgres, setPostgres] = useState(
    Boolean(current?.content.dependencies?.length),
  );
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await submit({
        expected_profile: current?.id ?? null,
        actor: data.get("actor"),
        reason: data.get("reason"),
        content: {
          image: data.get("image"),
          cpus: Number(data.get("cpus")),
          memory_mib: Number(data.get("memory")),
          pids_limit: Number(data.get("pids")),
          build_command: data.get("build"),
          verification_command: data.get("verify"),
          mounts,
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
    <form className="form-stack" onSubmit={save}>
      <p className="hint">
        先在本机构建包含 OpenCode 1.19.4 和已锁定 Superpowers
        的镜像。新版本只影响后续工作区；已有需求继续使用自己的固定版本。
      </p>
      <label>
        运行镜像
        <input
          name="image"
          defaultValue={defaults?.image ?? image}
          required
          maxLength={240}
        />
      </label>
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
          required
          defaultValue={defaults?.verification_command ?? "node --test"}
          maxLength={2000}
        />
      </label>
      <p className="hint">
        命令在需求容器中执行。复杂步骤写入仓库脚本；不支持管道、重定向或多条命令拼接。
      </p>
      <fieldset>
        <legend>只读配置文件</legend>
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
      <fieldset>
        <legend>业务依赖</legend>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={postgres}
            onChange={(event) => setPostgres(event.target.checked)}
          />
          为每个需求启用独立 PostgreSQL 17
        </label>
        <p className="hint">
          每个需求独立数据库、内部网络、数据卷和认证，不开放宿主端口。依赖占用一个容器名额；启动前需备好
          postgres:17-bookworm 镜像。停止会先保存快照并保留数据卷。
        </p>
      </fieldset>
      <label>
        登记人
        <input name="actor" required maxLength={160} />
      </label>
      <label>
        配置原因
        <textarea name="reason" required maxLength={4000} />
      </label>
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      <button className="primary" disabled={busy}>
        {busy ? "保存中…" : "保存新版本"}
      </button>
    </form>
  );
}
