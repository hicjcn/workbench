import { AgentCliPanel } from "./AgentCliPanel";
import { DockerImagesPanel } from "./DockerImagesPanel";
import { ContainersPanel } from "./ContainersPanel";
import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, active, labels, timeLabel, jobLabel } from "./api";
import type { Detail, Job, Project, Requirement } from "./api";
import { Modal, ProjectForm, RequirementForm } from "./Forms";
import { ExecutionLog } from "./ExecutionLog";
import { WorkflowPanel } from "./WorkflowPanel";
import { MaterialPanel } from "./MaterialPanel";
import { DeliveryPanel } from "./DeliveryPanel";
import { RuntimePanel } from "./RuntimePanel";
import { ProjectProfilePanel } from "./ProjectProfilePanel";
import { EnvironmentPanel } from "./EnvironmentPanel";
import { ApplicationPanel } from "./ApplicationPanel";
import { AcceptancePanel } from "./AcceptancePanel";

function Badge({ value }: { value: string }) {
  return (
    <span className={`badge state-${value.toLowerCase()}`}>
      {labels[value] ?? value}
    </span>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [tokenPath, setTokenPath] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [modal, setModal] = useState<"project" | "requirement" | null>(null);
  const [view, setView] = useState<
    "requirements" | "projects" | "deliveries" | "runtime" | "images" | "containers" | "agents"
  >(() => location.hash === "#agent-clis" ? "agents" : "requirements");
  const [task, setTask] = useState("");
  const [reason, setReason] = useState("");
  const [logId, setLogId] = useState("");
  const [model, setModel] = useState("");
  const [workflowEnabled, setWorkflowEnabled] = useState(false);
  useEffect(() => {
    let live = true;
    api<{ authenticated: boolean; token_file: string }>("/session")
      .then((value) => {
        if (live) {
          setAuthenticated(value.authenticated);
          setTokenPath(value.token_file);
        }
      })
      .catch((failure) => setError(failure.message));
    return () => {
      live = false;
    };
  }, []);
  const reload = useCallback(async () => {
    const [nextProjects, nextRequirements, configuration] = await Promise.all([
      api<Project[]>("/projects"),
      api<Requirement[]>("/requirements"),
      api<{ model: string }>("/config"),
    ]);
    setProjects(nextProjects);
    setRequirements(nextRequirements);
    setModel(configuration.model);
    setSelected((previous) => previous || nextRequirements[0]?.id || "");
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    const update = () =>
      void reload().catch((failure) => setError(failure.message));
    update();
    const timer = setInterval(update, 2500);
    return () => clearInterval(timer);
  }, [authenticated, reload]);
  useEffect(() => {
    if (!authenticated || !selected) {
      setDetail(null);
      return;
    }
    let live = true;
    setDetail(null);
    setLogId("");
    const update = () =>
      api<Detail>(`/requirements/${selected}`)
        .then((value) => {
          if (live) {
            setDetail(value);
            setLogId((previous) => previous || value.jobs[0]?.id || "");
          }
        })
        .catch((failure) => {
          if (live) setError(failure.message);
        });
    void update();
    const timer = setInterval(() => void update(), 1500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [authenticated, selected]);
  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/session", { token: token.trim() });
      setToken("");
      setAuthenticated(true);
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function action(kind: string, prompt = "") {
    if (!detail) return false;
    setBusy(true);
    setError("");
    try {
      const job = await api<Job>(`/requirements/${detail.id}/actions`, {
        action: kind,
        prompt,
      });
      setLogId(job.id);
      setDetail(await api<Detail>(`/requirements/${detail.id}`));
      await reload();
      return true;
    } catch (failure) {
      setError((failure as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function decide(decision: string) {
    if (!detail?.workspace.snapshot) return;
    setBusy(true);
    setError("");
    try {
      const job = await api<Job>(`/requirements/${detail.id}/reviews`, {
        digest: detail.workspace.snapshot.tree_digest,
        decision,
        reason,
      });
      setLogId(job.id);
      setDetail(await api<Detail>(`/requirements/${detail.id}`));
      setReason("");
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const running = detail?.jobs.find(active);
  const unknown = detail?.jobs.some((job) => job.status === "UNKNOWN");
  const currentLog = detail?.jobs.find((job) => job.id === logId) ?? null;
  const disabled = busy || Boolean(running) || Boolean(unknown);
  const snapshot = detail?.workspace.snapshot;
  if (authenticated !== true)
    return (
      <main className="login-page">
        <div className="login-card">
          <div className="brand-symbol">w.</div>
          <h1>你的开发工作台</h1>
          <p className="muted">连接本机环境，开始组织需求与 Agent 执行。</p>
          {authenticated === null && !error ? (
            <p>正在连接控制服务…</p>
          ) : (
            <form onSubmit={login} className="form-stack">
              <label>
                本机访问令牌
                <input
                  type="password"
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  required
                  autoComplete="off"
                  autoFocus
                />
              </label>
              <p className="hint">
                令牌保存在 <code>{tokenPath}</code>，仅用于访问此工作台。
              </p>
              <button className="primary" disabled={busy}>
                进入工作台
              </button>
            </form>
          )}
          {error ? (
            <p className="inline-error" role="alert">
              {error}
            </p>
          ) : null}
        </div>
      </main>
    );
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-symbol">w.</span>
          <div>
            <strong>开发工作台</strong>
            <span>WORKBENCH</span>
          </div>
        </div>
        <div className="topbar-right">
          <span className="version-pill">M10 · 本地运行</span>
          <button
            className="text-button"
            onClick={() =>
              void api("/session", undefined, "DELETE")
                .then(() => setAuthenticated(false))
                .catch((failure) => setError(failure.message))
            }
          >
            退出
          </button>
        </div>
      </header>
      <div className="page-heading">
        <div>
          <p className="eyebrow">DEVELOPMENT SPACE</p>
          <h1>每个需求，都有自己的工作区。</h1>
          <p className="muted">
            从需求到代码，让环境、执行过程与人工确认都有迹可循。
          </p>
        </div>
        <div className="heading-actions">
          <button className="secondary" onClick={() => setModal("project")}>
            ＋ 登记项目
          </button>
          <button
            className="primary"
            onClick={() =>
              setModal(projects.length ? "requirement" : "project")
            }
          >
            ＋ 新建需求
          </button>
        </div>
      </div>
      <div className="summary-strip">
        <div>
          <strong>{requirements.length}</strong>
          <span>全部需求</span>
        </div>
        <div>
          <strong>
            {
              requirements.filter(
                (item) => item.workspace.environment === "RUNNING",
              ).length
            }
          </strong>
          <span>运行环境</span>
        </div>
        <div>
          <strong>
            {
              requirements.filter(
                (item) => item.workspace.approval === "APPROVED",
              ).length
            }
          </strong>
          <span>自审通过</span>
        </div>
        <div className="model-note">
          <span>当前模型</span>
          <code>{model || "读取配置中"}</code>
        </div>
      </div>
      {error ? (
        <div className="error-banner" role="alert">
          <span>{error}</span>
          <button aria-label="关闭提示" onClick={() => setError("")}>
            ×
          </button>
        </div>
      ) : null}
      <nav className="view-tabs" aria-label="工作台页面">
        <button
          className={view === "requirements" ? "selected" : ""}
          aria-pressed={view === "requirements"}
          onClick={() => setView("requirements")}
        >
          需求工作区 <span>{requirements.length}</span>
        </button>
        <button
          className={view === "projects" ? "selected" : ""}
          aria-pressed={view === "projects"}
          onClick={() => setView("projects")}
        >
          项目登记 <span>{projects.length}</span>
        </button>
        <button
          className={view === "deliveries" ? "selected" : ""}
          aria-pressed={view === "deliveries"}
          onClick={() => setView("deliveries")}
        >
          跨项目交付
        </button>
        <button
          className={view === "runtime" ? "selected" : ""}
          aria-pressed={view === "runtime"}
          onClick={() => setView("runtime")}
        >
          资源与恢复
        </button>
        <button className={view === "images" ? "selected" : ""} aria-pressed={view === "images"} onClick={() => setView("images")}>Docker 镜像</button>
        <button className={view === "containers" ? "selected" : ""} aria-pressed={view === "containers"} onClick={() => setView("containers")}>容器管理</button>
        <button className={view === "agents" ? "selected" : ""} aria-pressed={view === "agents"} onClick={() => setView("agents")}>Agent CLI 配置</button>
      </nav>
      {view === "agents" ? <AgentCliPanel configureProjects={() => setView("projects")} openContainers={() => setView("containers")} /> : view === "images" ? <DockerImagesPanel configureProjects={() => setView("projects")} /> : view === "containers" ? <ContainersPanel requirements={requirements} projects={projects} configureProjects={() => setView("projects")} openRequirement={(id) => { setSelected(id); setView("requirements"); }} /> : view === "deliveries" ? (
        <DeliveryPanel
          requirements={requirements}
          projects={projects}
          openRequirement={(id) => {
            setSelected(id);
            setView("requirements");
          }}
        />
      ) : view === "runtime" ? (
        <RuntimePanel />
      ) : view === "projects" ? (
        <section className="projects-grid">
          {projects.length ? (
            projects.map((project) => (
              <article className="panel project-card" key={project.id}>
                <div className="project-icon">⌘</div>
                <h2>{project.name}</h2>
                <p className="muted">源码目录</p>
                <code className="path">{project.source_path}</code>
                <p className="muted">运行镜像</p>
                <code>{project.image}</code>
                <ProjectProfilePanel project={project} />
              </article>
            ))
          ) : (
            <div className="panel empty-state">
              <h2>先登记一个项目</h2>
              <p>选择现有 Git 仓库，后续每个需求都会从它创建独立副本。</p>
              <button className="primary" onClick={() => setModal("project")}>
                登记项目
              </button>
            </div>
          )}
        </section>
      ) : (
        <main className="workspace-layout">
          <aside className="requirement-list">
            <div className="list-caption">
              <strong>需求列表</strong>
              <span>{requirements.length} 项</span>
            </div>
            {requirements.length ? (
              requirements.map((item) => (
                <button
                  key={item.id}
                  className={`requirement-card ${selected === item.id ? "selected" : ""}`}
                  onClick={() => {
                    setSelected(item.id);
                    setError("");
                  }}
                >
                  <div className="card-meta">
                    <span>
                      {projects.find(
                        (project) => project.id === item.project_id,
                      )?.name ?? "项目"}
                    </span>
                    <Badge
                      value={
                        item.workspace.approval === "APPROVED"
                          ? "APPROVED"
                          : item.workspace.environment
                      }
                    />
                  </div>
                  <strong>{item.title}</strong>
                  <p>{item.description || "尚未补充需求背景"}</p>
                  <time>{timeLabel(item.created_at)}</time>
                </button>
              ))
            ) : (
              <div className="list-empty">
                <span className="empty-icon">▱</span>
                <strong>等待第一个需求</strong>
                <p>把要做的事情写下来，从一个清晰的目标开始。</p>
              </div>
            )}
            <p className="sidebar-note">
              每个需求独立保存代码、Agent 会话和运行环境。
            </p>
          </aside>
          {detail ? (
            <div className="detail-column">
              <section className="panel detail-heading">
                <div className="section-kicker">
                  {detail.project.name}
                  <span>独立开发</span><span>{detail.agent_cli?.name ?? "OpenCode"} · v{detail.agent_cli?.number ?? 1}{detail.agent_cli?.fixed ? "（已固定）" : "（准备时固定）"}</span>
                </div>
                <h2>{detail.title}</h2>
                <p>{detail.description || "尚未补充需求背景"}</p>
                {detail.acceptance ? (
                  <div className="acceptance">
                    <strong>验收标准</strong>
                    <p>{detail.acceptance}</p>
                  </div>
                ) : null}
              </section>
              <EnvironmentPanel
                detail={detail}
                disabled={disabled}
                action={action}
              />
              <ApplicationPanel
                detail={detail}
                disabled={disabled}
                action={action}
              />
              <WorkflowPanel
                key={detail.id}
                detail={detail}
                disabled={disabled}
                onEnabled={setWorkflowEnabled}
                onJob={(job) => setLogId(job.id)}
                onChanged={async () => {
                  setDetail(await api<Detail>(`/requirements/${detail.id}`));
                  await reload();
                }}
              />
              <AcceptancePanel
                key={`acceptance-${detail.id}`}
                requirementId={detail.id}
              />
              <MaterialPanel
                key={`materials-${detail.id}`}
                detail={detail}
                disabled={disabled}
                onJob={(job) => setLogId(job.id)}
                onChanged={async () => {
                  setDetail(await api<Detail>(`/requirements/${detail.id}`));
                  await reload();
                }}
              />
              <section className="panel">
                <div className="section-title">
                  <h2>工作区与环境</h2>
                  <Badge value={detail.workspace.environment} />
                </div>
                <div className="workspace-path">
                  <span>代码副本</span>
                  <code>{detail.workspace.path}/repo</code>
                </div>
                <div className="button-row">
                  <button
                    className="secondary"
                    disabled={
                      disabled || detail.workspace.environment !== "NOT_CREATED"
                    }
                    onClick={() => void action("prepare")}
                  >
                    创建工作区
                  </button>
                  <button
                    className="primary"
                    disabled={
                      disabled ||
                      detail.workspace.environment === "NOT_CREATED" ||
                      detail.workspace.environment === "RUNNING"
                    }
                    onClick={() => void action("start")}
                  >
                    启动环境
                  </button>
                  <button
                    className="secondary"
                    disabled={
                      busy ||
                      Boolean(running) ||
                      (!unknown &&
                        (detail.workspace.environment === "NOT_CREATED" ||
                          detail.workspace.environment === "STOPPED"))
                    }
                    onClick={() => void action(unknown ? "reconcile" : "stop")}
                  >
                    {unknown ? "停止并核实" : "停止环境"}
                  </button>
                  <button
                    className="text-button"
                    disabled={
                      disabled || detail.workspace.environment === "NOT_CREATED"
                    }
                    onClick={() => void action("refresh")}
                  >
                    刷新差异
                  </button>
                </div>
                {running ? (
                  <p className="operation-note">
                    ◌ {jobLabel(running)} · {labels[running.status]}
                    <button
                      className="text-button"
                      disabled={busy || running.cancel_requested}
                      onClick={() =>
                        void api(`/jobs/${running.id}/cancel`, {})
                          .then(() => reload())
                          .catch((failure) => setError(failure.message))
                      }
                    >
                      {running.cancel_requested ? "正在停止…" : "取消操作"}
                    </button>
                  </p>
                ) : null}
                <p className="hint">
                  停止环境会保留代码与 Agent
                  数据。执行状态不明时，先停止并核实。
                </p>
              </section>
              {!workflowEnabled ? (
                <section className="panel">
                  <div className="section-title">
                    <h2>交给 Agent</h2>
                    <span className="small-label">授权环境内执行</span>
                  </div>
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      void action("task", task).then((ok) => {
                        if (ok) setTask("");
                      });
                    }}
                  >
                    <label className="sr-only" htmlFor="task-prompt">
                      Agent 任务
                    </label>
                    <textarea
                      id="task-prompt"
                      rows={3}
                      value={task}
                      onChange={(event) => setTask(event.target.value)}
                      placeholder="描述这次需要分析、修改或验证的内容…"
                      maxLength={32000}
                    />
                    <div className="task-footer">
                      <span>结果与代码差异会保留，供你检查。</span>
                      <button
                        className="primary"
                        disabled={
                          disabled ||
                          detail.workspace.environment !== "RUNNING" ||
                          !task.trim()
                        }
                      >
                        运行任务 ↗
                      </button>
                    </div>
                  </form>
                </section>
              ) : null}
              <section className="panel">
                <div className="section-title">
                  <h2>执行记录</h2>
                  <span className="small-label">
                    {detail.jobs.length} 次操作
                  </span>
                </div>
                <div className="job-tabs">
                  {detail.jobs.map((job) => (
                    <button
                      key={job.id}
                      className={logId === job.id ? "selected" : ""}
                      onClick={() => setLogId(job.id)}
                    >
                      {jobLabel(job)}
                      <Badge value={job.status} />
                    </button>
                  ))}
                </div>
                <ExecutionLog job={currentLog} />
              </section>
              <section className="panel">
                <div className="section-title">
                  <h2>代码差异与自审</h2>
                  <Badge value={detail.workspace.approval} />
                </div>
                {snapshot ? (
                  <>
                    <div className="snapshot-meta">
                      <span>
                        代码快照{" "}
                        <code>{snapshot.tree_digest.slice(0, 12)}</code>
                      </span>
                      <span>{snapshot.files.length} 个源码文件</span>
                    </div>
                    <pre className="diff" aria-label="代码差异">
                      {detail.workspace.diff
                        ? detail.workspace.diff
                            .split("\n")
                            .map((line, index) => (
                              <span
                                key={index}
                                className={
                                  line.startsWith("+")
                                    ? "addition"
                                    : line.startsWith("-")
                                      ? "deletion"
                                      : ""
                                }
                              >
                                {line}
                                {"\n"}
                              </span>
                            ))
                        : "当前代码与初始快照一致。"}
                    </pre>
                    {detail.workspace.diff_truncated ? (
                      <p className="inline-error">
                        差异超出当前界面限制，暂不能在此确认。
                      </p>
                    ) : null}
                    {!workflowEnabled ? (
                      <>
                        <label className="review-reason">
                          审查意见
                          <input
                            value={reason}
                            onChange={(event) => setReason(event.target.value)}
                            placeholder="记录确认依据；退回时请说明原因"
                            maxLength={4000}
                          />
                        </label>
                        <div className="button-row">
                          <button
                            className="primary"
                            disabled={
                              disabled || detail.workspace.diff_truncated
                            }
                            onClick={() => void decide("APPROVED")}
                          >
                            自审通过
                          </button>
                          <button
                            className="secondary"
                            disabled={
                              disabled ||
                              detail.workspace.diff_truncated ||
                              !reason.trim()
                            }
                            onClick={() => void decide("REJECTED")}
                          >
                            退回修改
                          </button>
                          <span className="hint">
                            确认绑定当前代码快照，代码变化后需重新确认。
                          </span>
                        </div>
                      </>
                    ) : (
                      <p className="hint">
                        已开启默认流程，请在完成模型评审与问题处理后，从流程面板进行自审。
                      </p>
                    )}
                    {detail.reviews.length ? (
                      <div className="review-history">
                        {detail.reviews.map((review) => (
                          <div key={review.id}>
                            <Badge value={review.decision} />
                            <span>
                              {review.reason || "开发者已检查当前快照"}
                            </span>
                            <time>{timeLabel(review.created_at)}</time>
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </>
                ) : (
                  <div className="muted empty-diff">
                    创建工作区后，可以查看代码快照和差异。
                  </div>
                )}
              </section>
            </div>
          ) : (
            <section className="panel welcome-panel">
              <div className="welcome-graphic">
                <span>▱</span>
                <i></i>
                <span>⌘</span>
                <i></i>
                <span>✓</span>
              </div>
              <p className="eyebrow">YOUR NEXT STEP</p>
              <h2>准备好，开始一个清晰的需求。</h2>
              <p className="muted">
                登记项目、创建独立工作区，再把具体任务交给 Agent。
                <br />
                你可以随时检查执行记录和代码差异。
              </p>
              <button
                className="primary"
                onClick={() =>
                  setModal(projects.length ? "requirement" : "project")
                }
              >
                {projects.length ? "新建需求" : "登记第一个项目"}
              </button>
            </section>
          )}
        </main>
      )}
      <footer className="page-footer">
        <span>WORKBENCH / M10</span>
        <span>源码独立 · 过程可追溯 · 结果由你确认</span>
      </footer>
      {modal ? (
        <Modal
          title={modal === "project" ? "登记项目" : "新建需求"}
          close={() => setModal(null)}
        >
          {modal === "project" ? (
            <ProjectForm
              submit={async (value) => {
                await api("/projects", value);
                await reload();
                setModal(null);
                setView("projects");
              }}
            />
          ) : (
            <RequirementForm
              projects={projects}
              submit={async (value) => {
                const created = await api<Requirement>("/requirements", value);
                await reload();
                setSelected(created.id);
                setView("requirements");
                setModal(null);
              }}
            />
          )}
        </Modal>
      ) : null}
    </div>
  );
}
