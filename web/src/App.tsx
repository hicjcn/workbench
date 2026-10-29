import { AgentCliPanel } from "./AgentCliPanel";
import { DockerImagesPanel } from "./DockerImagesPanel";
import { ContainersPanel } from "./ContainersPanel";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, labels, timeLabel } from "./api";
import type { Detail, Project, Requirement } from "./api";
import { Modal, ProjectForm, RequirementForm } from "./Forms";
import { DeliveryPanel } from "./DeliveryPanel";
import { RequirementDetail } from "./RequirementDetail";
import { RequirementEditForm } from "./RequirementEditForm";
import { RuntimePanel } from "./RuntimePanel";
import { ProjectProfilePanel } from "./ProjectProfilePanel";

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
  const [modal, setModal] = useState<
    "project" | "project-edit" | "requirement" | null
  >(null);
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const [editingRequirementId, setEditingRequirementId] = useState<
    string | null
  >(null);
  const [detailVersion, setDetailVersion] = useState(0);
  const [view, setView] = useState<
    | "requirements"
    | "projects"
    | "deliveries"
    | "runtime"
    | "images"
    | "containers"
    | "agents"
  >(() => (location.hash === "#agent-clis" ? "agents" : "requirements"));
  const [model, setModel] = useState("");
  const [defaultImage, setDefaultImage] = useState("agent-workbench-m1:local");
  const reloadRequest = useRef(0);
  const detailRequest = useRef(0);
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
    const requested = ++reloadRequest.current;
    const [nextProjects, nextRequirements, configuration] = await Promise.all([
      api<Project[]>("/projects"),
      api<Requirement[]>("/requirements"),
      api<{ model: string; default_image?: string }>("/config"),
    ]);
    if (requested !== reloadRequest.current) return;
    setProjects(nextProjects);
    setRequirements(nextRequirements);
    setModel(configuration.model);
    setDefaultImage(configuration.default_image ?? "agent-workbench-m1:local");
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
      detailRequest.current++;
      setDetail(null);
      return;
    }
    let live = true;
    setDetail(null);
    const update = () => {
      const requested = ++detailRequest.current;
      return api<Detail>(`/requirements/${selected}`)
        .then((value) => {
          if (live && requested === detailRequest.current) {
            setDetail(value);
          }
        })
        .catch((failure) => {
          if (live && requested === detailRequest.current)
            setError(failure.message);
        });
    };
    void update();
    const timer = setInterval(() => void update(), 1500);
    return () => {
      live = false;
      detailRequest.current++;
      clearInterval(timer);
    };
  }, [authenticated, selected]);
  const refreshDetail = useCallback(async () => {
    if (!selected) return;
    const requested = ++detailRequest.current;
    const next = await api<Detail>(`/requirements/${selected}`);
    if (requested === detailRequest.current)
      setDetail((previous) => (previous?.id === next.id ? next : previous));
    await reload();
  }, [selected, reload]);
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
          <span>默认模型</span>
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
        <button
          className={view === "images" ? "selected" : ""}
          aria-pressed={view === "images"}
          onClick={() => setView("images")}
        >
          Docker 镜像
        </button>
        <button
          className={view === "containers" ? "selected" : ""}
          aria-pressed={view === "containers"}
          onClick={() => setView("containers")}
        >
          容器管理
        </button>
        <button
          className={view === "agents" ? "selected" : ""}
          aria-pressed={view === "agents"}
          onClick={() => setView("agents")}
        >
          Agent CLI 配置
        </button>
      </nav>
      {view === "agents" ? (
        <AgentCliPanel
          configureProjects={() => setView("projects")}
          openContainers={() => setView("containers")}
        />
      ) : view === "images" ? (
        <DockerImagesPanel configureProjects={() => setView("projects")} />
      ) : view === "containers" ? (
        <ContainersPanel
          requirements={requirements}
          projects={projects}
          configureProjects={() => setView("projects")}
          openRequirement={(id) => {
            setSelected(id);
            setView("requirements");
          }}
        />
      ) : view === "deliveries" ? (
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
                <div className="section-title">
                  <h2>{project.name}</h2>
                  <button
                    className="text-button"
                    aria-label={`修改项目 ${project.name}`}
                    onClick={() => {
                      setEditingProject(project);
                      setModal("project-edit");
                    }}
                  >
                    修改登记
                  </button>
                </div>
                <p className="muted">源码目录</p>
                <code className="path">{project.source_path}</code>
                {Boolean(project.repositories?.length) && (
                  <div className="repository-options">
                    <p className="hint">
                      多仓库业务项目 · {project.repositories!.length} 个仓库
                    </p>
                    {project.repositories!.map((path) => (
                      <code className="path" key={path}>
                        {path}
                      </code>
                    ))}
                    <p className="hint">
                      共享文件：{project.shared_files?.join("、") || "未选择"}
                    </p>
                  </div>
                )}
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
                  aria-pressed={selected === item.id}
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
            <RequirementDetail
              key={`${detail.id}-${detailVersion}`}
              detail={detail}
              onChanged={refreshDetail}
              onEdit={() => setEditingRequirementId(detail.id)}
              configureProject={() => setView("projects")}
            />
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
      {editingRequirementId && (
        <Modal
          title="修改需求"
          wide
          close={() => setEditingRequirementId(null)}
        >
          <RequirementEditForm
            key={editingRequirementId}
            requirementId={editingRequirementId}
            onSaved={async () => {
              const editedId = editingRequirementId;
              setEditingRequirementId(null);
              setView("requirements");
              setSelected(editedId);
              setDetailVersion((previous) => previous + 1);
              try {
                if (selected === editedId) await refreshDetail();
                else await reload();
              } catch (failure) {
                setError((failure as Error).message);
              }
            }}
          />
        </Modal>
      )}
      {modal ? (
        <Modal
          title={
            modal === "project-edit"
              ? "修改项目登记"
              : modal === "project"
                ? "登记项目"
                : "新建需求"
          }
          wide={modal !== "requirement"}
          close={() => setModal(null)}
        >
          {modal !== "requirement" ? (
            <ProjectForm
              key={modal === "project-edit" ? editingProject?.id : "new"}
              project={
                modal === "project-edit"
                  ? (editingProject ?? undefined)
                  : undefined
              }
              defaultImage={defaultImage}
              submit={async (value) => {
                if (modal === "project-edit" && editingProject) {
                  await api(`/projects/${editingProject.id}`, value, "PUT");
                  if (detail?.project_id === editingProject.id) {
                    setDetail(await api<Detail>(`/requirements/${detail.id}`));
                  }
                } else {
                  await api("/projects", value);
                }
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
