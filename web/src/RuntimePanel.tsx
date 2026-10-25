import { useEffect, useState } from "react";
import { api, timeLabel } from "./api";

type Status = {
  limits: { active_jobs: number; containers: number };
  jobs: Record<string, number>;
  data_dir: string;
  disk_free_bytes: number;
  resources_stale: boolean;
  resources: {
    updated_at: number;
    report: {
      available: boolean;
      error?: string;
      containers: {
        id: string;
        name: string;
        workspace: string;
        kind?: string;
        orphan: boolean;
        verified: boolean;
        running: boolean;
        memory_limit: number;
        cpu_limit: number;
      }[];
    };
  } | null;
  backups: {
    id: string;
    sha256: string;
    size: number;
    created_at: number;
    available: boolean;
  }[];
  restored: {
    restored_at: number;
    credentials_regenerated: boolean;
    sessions_replayed: boolean;
    workspace_count: number;
    archive_sha256: string;
  } | null;
};
export function RuntimePanel() {
  const [state, setState] = useState<Status | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const update = () =>
      void api<Status>("/runtime")
        .then((value) => {
          if (live) {
            setState(value);
            setError("");
          }
        })
        .catch((failure) => {
          if (live) setError(failure.message);
        });
    update();
    const timer = setInterval(update, 2500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);
  const root = state?.data_dir ?? "/path/to/data";
  const quoted = "'" + root.replaceAll("'", "'\\''") + "'";
  const command = "agent-workbench-maintenance";
  return (
    <main className="delivery-space">
      <div className="section-title">
        <div>
          <h2>资源与恢复</h2>
          <p className="muted">
            查看当前工作台的资源；停止环境时保留代码、材料与执行证据。
          </p>
        </div>
      </div>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      {state && (
        <>
          <section className="panel">
            <h3>运行概况</h3>
            <div className="delivery-gates">
              <span>
                执行中 {state.jobs.RUNNING} / {state.limits.active_jobs}
              </span>
              <span>排队 {state.jobs.QUEUED}</span>
              <span>待核实 {state.jobs.UNKNOWN}</span>
              <span>容器上限 {state.limits.containers}</span>
              <span>
                磁盘可用 {(state.disk_free_bytes / 1024 ** 3).toFixed(1)} GiB
              </span>
            </div>
            <code className="path">{state.data_dir}</code>
            <p className="hint">
              {state.resources_stale
                ? "资源报告已过期或尚未收到，请检查 Runner。"
                : `Runner 报告于 ${timeLabel(state.resources!.updated_at)}`}
            </p>
            {state.resources?.report.error && (
              <p role="alert">{state.resources.report.error}</p>
            )}
            {state.resources?.report.containers?.map((item) => (
              <div className="delivery-member" key={item.id}>
                <strong>
                  {item.name} · {item.running ? "运行中" : "已停止"}
                  {item.orphan ? " · 缺少运行记录" : ""}
                  {!item.verified ? " · 归属待核实" : ""}
                </strong>
                <code className="path">{item.workspace}</code>
                <p className="hint">
                  {item.kind === "application"
                    ? "应用服务"
                    : item.kind === "postgres"
                      ? "PostgreSQL"
                      : "Agent 环境"}{" "}
                  · {item.cpu_limit} CPU ·{" "}
                  {(item.memory_limit / 1024 ** 2).toFixed(0)} MiB
                </p>
              </div>
            ))}
            {state.resources?.report.available &&
              !state.resources.report.containers.length && (
                <p>没有托管容器。</p>
              )}
          </section>
          <section className="panel">
            <h3>离线维护</h3>
            <p>
              先停止 API 和
              Runner，再在本机终端执行。维护锁会阻止服务运行时备份；停止操作只清理本目录可核实归属的资源。
            </p>
            <pre className="maintenance-commands">{`${command} inspect --data-dir ${quoted}\n${command} stop --data-dir ${quoted}\n${command} backup --data-dir ${quoted}\n\n# 恢复到尚不存在的新目录\n${command} restore --archive '/path/to/backup.zip' --destination '/path/to/new-data'`}</pre>
            <p className="hint">
              备份包含源码与 Git 历史，应私密保存。访问令牌、Agent
              认证、运行密码不复制；恢复会生成新令牌，需重新配置模型认证。内置
              PostgreSQL 数据快照随备份保存，其他外部数据库需独立备份。
            </p>
          </section>
          <section className="panel">
            <h3>已验证的备份</h3>
            {state.backups.length ? (
              state.backups.map((item) => (
                <div className="delivery-member" key={item.id}>
                  <strong>
                    {timeLabel(item.created_at)} ·{" "}
                    {item.available ? "文件存在" : "文件已移走"}
                  </strong>
                  <code className="path">backups/{item.id}.zip</code>
                  <p className="hint">
                    {(item.size / 1024 ** 2).toFixed(1)} MiB
                  </p>
                  <code className="path">SHA-256 {item.sha256}</code>
                </div>
              ))
            ) : (
              <p className="muted">尚无本机备份记录。</p>
            )}
          </section>
          {state.restored && (
            <section className="panel">
              <h3>本目录恢复记录</h3>
              <p>
                {timeLabel(state.restored.restored_at)} ·{" "}
                {state.restored.workspace_count} 个代码副本 ·{" "}
                {state.restored.credentials_regenerated
                  ? "已生成新令牌"
                  : "请核对认证"}{" "}
                ·{" "}
                {state.restored.sessions_replayed
                  ? "请检查任务"
                  : "未自动重放任务"}
              </p>
              <code className="path">{state.restored.archive_sha256}</code>
            </section>
          )}
        </>
      )}
    </main>
  );
}
