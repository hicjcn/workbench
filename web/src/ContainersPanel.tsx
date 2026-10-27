import { RuntimeProfileSync } from "./RuntimeProfileSync";
import { RuntimeConfigurationSummary } from "./RuntimeConfigurationSummary";
import { useEffect, useState } from "react";
import { api, active, jobLabel, labels, timeLabel } from "./api";
import type { Detail, Job, Project, Requirement } from "./api";
import type { DockerState } from "./dockerTypes";
import { ExecutionLog } from "./ExecutionLog";
export function ContainersPanel({ requirements, projects, openRequirement, configureProjects }: { requirements: Requirement[]; projects: Project[]; openRequirement: (id: string) => void; configureProjects: () => void }) {
  const [state, setState] = useState<DockerState | null>(null);
  const [selected, setSelected] = useState(() => requirements[0]?.id ?? "");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [logId, setLogId] = useState("");
  useEffect(() => {
    let live = true;
    setDetail(null); setLogId("");
    const update = async () => {
      try {
        const [next, req] = await Promise.all([api<DockerState>("/docker"), selected ? api<Detail>(`/requirements/${selected}`) : Promise.resolve(null)]);
        if (live) { setState(next); setDetail(req); }
      } catch (failure) { if (live) setError((failure as Error).message); }
    };
    void update(); const timer = setInterval(() => void update(), 2000);
    return () => { live = false; clearInterval(timer); };
  }, [selected, refresh]);
  const current = detail?.jobs.find(active);
  const unknown = detail?.jobs.some(job => job.status === "UNKNOWN");
  const blocked = busy || Boolean(current) || Boolean(unknown);
  async function run(action: string) {
    if (!detail) return; setBusy(true); setError("");
    try { const job = await api<Job>(`/requirements/${detail.id}/actions`, { action, prompt: "" }); setLogId(job.id); setDetail(await api<Detail>(`/requirements/${detail.id}`)); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  const environment = detail?.workspace.environment;
  const resources = state?.containers.filter(item => item.workspace === detail?.workspace.path) ?? [];
  const log = detail?.jobs.find(job => job.id === logId) ?? detail?.jobs[0] ?? null;
  return <main className="docker-space">
    <div className="section-title docker-heading"><div><p className="eyebrow">DOCKER / CONTAINERS</p><h2>容器管理</h2><p className="muted">集中管理需求的 Agent 环境、应用服务与数据库，查看资源和执行记录。</p></div><div className="button-row"><button className="secondary" onClick={configureProjects}>运行与挂载配置</button><button className="secondary" onClick={() => setRefresh(value => value + 1)}>刷新状态</button></div></div>
    <div className="docker-status"><span className={`connection-dot ${state?.connected ? "connected" : ""}`} />{state?.trial ? "站点试用 · 容器均为静态示例，未实际启动" : state?.connected ? "Runner 已连接 Docker" : "未连接或 Runner 报告已过期"}<span className="hint">{state?.updated_at ? `报告 ${timeLabel(state.updated_at)}` : "等待本机执行服务"}</span></div>
    {error && <p role="alert" className="inline-error">{error}</p>}
    <section className="docker-stats"><div><strong>{state?.containers.length ?? "—"}</strong><span>{state?.trial ? "示例容器" : "托管容器"}</span></div><div><strong>{state?.containers.filter(item => item.running).length ?? "—"}</strong><span>{state?.trial ? "示例运行状态" : "运行中"}</span></div><div><strong>{requirements.length}</strong><span>关联需求</span></div></section>
    <div className="docker-container-layout"><aside className="panel docker-requirements"><h3>需求环境</h3><input aria-label="搜索需求环境" placeholder="搜索需求或项目" value={filter} onChange={event => setFilter(event.target.value)} />{requirements.filter(req => (req.title + projects.find(project => project.id === req.project_id)?.name).toLowerCase().includes(filter.toLowerCase())).map(req => <button key={req.id} className={`requirement-card ${selected === req.id ? "selected" : ""}`} aria-pressed={selected === req.id} onClick={() => { setSelected(req.id); setError(""); }}><span className="muted">{projects.find(project => project.id === req.project_id)?.name}</span><strong>{req.title}</strong><span className="small-label">{labels[req.workspace.environment] ?? req.workspace.environment}</span></button>)}{!requirements.length && <p className="muted">先新建需求，再创建独立工作区。</p>}</aside>
      <div className="docker-container-detail">{detail ? <><section className="panel"><div className="section-title"><div><h3>{detail.title}</h3><p className="hint">{detail.project.name}</p></div><button className="text-button" onClick={() => openRequirement(detail.id)}>进入需求 ↗</button></div><p className="hint">Agent CLI：{detail.agent_cli?.name ?? "OpenCode"} · v{detail.agent_cli?.number ?? 1} · {detail.agent_cli?.fixed ? "工作区版本已固定" : "准备工作区时固定"}</p><RuntimeProfileSync detail={detail} disabled={blocked} action={() => void run("sync_profile")} /><div className="delivery-gates"><span>环境：{labels[environment ?? ""] ?? environment}</span><span>应用：{detail.application?.phase ?? "未启动"}</span><span>数据库：{detail.dependency?.phase ?? "未配置"}</span></div><div className="button-row docker-lifecycle"><button className="secondary" disabled={blocked || environment !== "NOT_CREATED"} onClick={() => void run("prepare")}>创建工作区</button><button className="secondary" disabled={blocked || !detail.workspace.snapshot || environment === "RUNNING" || !detail.profile} onClick={() => void run("preflight")}>环境预检</button><button className="primary" disabled={blocked || environment === "NOT_CREATED" || environment === "RUNNING"} onClick={() => void run("start")}>启动容器环境</button><button className="secondary" disabled={blocked || environment !== "RUNNING"} onClick={() => void run("stop")}>停止容器环境</button><button className="text-button" disabled={busy || Boolean(current) || (!unknown && environment !== "UNKNOWN")} onClick={() => void run("reconcile")}>停止并核实</button></div><p className="hint">按顺序创建工作区、预检、启动。停止先停止应用并保存数据库快照，保留源码、会话和数据卷。需使用固定版本时在项目运行配置中设置镜像。</p>{current && <p className="operation-note">{jobLabel(current)} · {labels[current.status]}</p>}<div className="docker-config-grid"><div><span>运行镜像</span><code>{detail.profile?.content.image ?? detail.project.image}</code></div><div><span>资源限制</span><code>{detail.profile ? `${detail.profile.content.cpus} CPU / ${detail.profile.content.memory_mib} MiB / ${detail.profile.content.pids_limit} PIDs` : "默认 1 CPU / 1536 MiB / 256 PIDs"}</code></div><div><span>工作区挂载</span><code>{detail.workspace.path}/repo → /workspace/project</code></div><div><span>只读配置挂载</span>{detail.profile?.content.mounts.length ? detail.profile.content.mounts.map(mount => <code key={mount.target}>{mount.source} → {mount.target}（只读）</code>) : <code>无额外配置挂载</code>}</div><RuntimeConfigurationSummary detail={detail} /></div><div className="button-row"><button className="secondary" disabled={blocked || !detail.profile?.content.application || environment === "NOT_CREATED"} onClick={() => void run("app_start")}>启动应用服务</button><button className="secondary" disabled={blocked || !detail.application} onClick={() => void run("app_stop")}>停止应用服务</button><button className="text-button" disabled={blocked || !detail.application} onClick={() => void run("app_logs")}>查看应用日志</button></div></section>
        <section className="panel"><h3>当前需求的托管容器</h3>{resources.length ? resources.map(item => <article className="docker-operation" key={item.id}><div className="section-title"><strong>{item.name}</strong><span className="badge">{state?.trial ? "示例 · " : ""}{item.running ? "运行中" : "已停止"}</span></div><p className="hint">{item.kind === "postgres" ? "PostgreSQL" : item.kind === "application" ? "应用服务" : "Agent / OpenCode"} · {item.cpu_limit} CPU · {(item.memory_limit / 1024 ** 2).toFixed(0)} MiB · {item.verified ? "归属已核实" : "归属待核实"}</p><code className="path">{item.image_id}</code><p className="hint">网络：{item.networks.join("、") || "无"}{item.orphan ? " · 缺少运行记录，请停止并核实" : ""}</p></article>) : <p className="docker-empty">{state?.connected ? "该需求当前没有托管容器。" : "尚未收到此需求的有效容器报告。"}</p>}</section>
        <section className="panel"><h3>启动、停止与应用日志</h3><div className="job-tabs">{detail.jobs.map(job => <button key={job.id} className={log?.id === job.id ? "selected" : ""} onClick={() => setLogId(job.id)}>{jobLabel(job)} · {labels[job.status] ?? job.status}</button>)}</div><ExecutionLog job={log} /></section></> : <section className="panel docker-empty">{selected ? "正在读取需求环境…" : "选择一个需求查看环境与容器。"}</section>}</div>
    </div>
    {Boolean(state?.containers.some(item => !requirements.some(req => req.workspace.path === item.workspace))) && <section className="panel"><h3>其他托管资源与只读执行副本</h3>{state?.containers.filter(item => !requirements.some(req => req.workspace.path === item.workspace)).map(item => <p key={item.id}><code>{item.name}</code> · {item.kind} · {item.verified ? "归属已核实" : "待核实"} · {item.running ? "运行中" : "已停止"}</p>)}<p className="hint">只读分析、模型评审副本由所属任务管理；回到需求的执行记录处理取消或核实。</p></section>}
  </main>;
}
