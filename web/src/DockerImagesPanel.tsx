import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, timeLabel } from "./api";
import { Modal } from "./Forms";
import type { DockerImage, DockerOperation, DockerRecipe, DockerState } from "./dockerTypes";
import { imageActions, operationStatus } from "./dockerTypes";

export function DockerImagesPanel({ configureProjects }: { configureProjects: () => void }) {
  const [state, setState] = useState<DockerState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("");
  const [tab, setTab] = useState<"images" | "recipes">("images");
  const [editor, setEditor] = useState<{ recipe: DockerRecipe | null } | null>(null);
  const [operation, setOperation] = useState<{ kind: string; reference: string; recipe?: DockerRecipe; image?: DockerImage } | null>(null);
  const [acknowledge, setAcknowledge] = useState<DockerOperation | null>(null);
  const [history, setHistory] = useState<string | null>(null);
  const refresh = useCallback(async () => setState(await api<DockerState>("/docker")), []);
  useEffect(() => {
    let live = true;
    const update = () => void api<DockerState>("/docker").then(value => { if (live) setState(value); }).catch(failure => { if (live) setError(failure.message); });
    update(); const timer = setInterval(update, 3000);
    return () => { live = false; clearInterval(timer); };
  }, []);
  const pending = state?.operations.some(item => ["QUEUED", "RUNNING", "UNKNOWN"].includes(item.status));
  async function cancel(item: DockerOperation) {
    setBusy(true); setError("");
    try { await api(`/docker/operations/${item.id}/cancel`, {}); await refresh(); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!operation) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try {
      await api("/docker/operations", { kind: operation.kind, reference: String(form.get("reference")), recipe_id: operation.recipe?.id ?? null, expected_image_id: operation.image?.id ?? null, actor: String(form.get("actor")), reason: String(form.get("reason")) });
      setOperation(null); await refresh();
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  return <main className="docker-space">
    <div className="section-title docker-heading"><div><p className="eyebrow">DOCKER / IMAGES</p><h2>Docker 镜像</h2><p className="muted">维护环境 Dockerfile，构建版本，拉取基础镜像，再将镜像用于项目运行配置。</p></div><div className="button-row"><button className="secondary" disabled={busy || pending} onClick={() => { setError(""); setOperation({ kind: "pull", reference: "" }); }}>拉取镜像</button><button className="primary" onClick={() => { setError(""); setEditor({ recipe: null }); }}>＋ 新建 Dockerfile</button></div></div>
    <div className="docker-status"><span className={`connection-dot ${state?.connected ? "connected" : ""}`} />{state?.trial ? "站点试用 · 展示示例镜像，未连接 Docker" : state?.connected ? "Runner 已连接 Docker" : "尚未收到有效 Runner 报告"}{state?.updated_at && <span className="hint">更新于 {timeLabel(state.updated_at)}</span>}<button className="text-button" onClick={() => void refresh().catch(failure => setError(failure.message))}>刷新列表</button></div>
    {error && !operation && !editor && !acknowledge && <p className="inline-error" role="alert">{error}</p>}
    <section className="docker-stats"><div><strong>{state?.images.length ?? "—"}</strong><span>{state?.trial ? "示例镜像" : "本机镜像"}</span></div><div><strong>{state?.recipes.filter(item => item.current).length ?? "—"}</strong><span>Dockerfile 模板</span></div><div><strong>{state?.operations.filter(item => ["QUEUED", "RUNNING"].includes(item.status)).length ?? "—"}</strong><span>维护任务</span></div></section>
    <section className="panel"><div className="docker-toolbar"><div className="button-row"><button className={tab === "images" ? "primary" : "secondary"} aria-pressed={tab === "images"} onClick={() => setTab("images")}>镜像列表</button><button className={tab === "recipes" ? "primary" : "secondary"} aria-pressed={tab === "recipes"} onClick={() => setTab("recipes")}>Dockerfile 与版本</button></div><input aria-label="搜索镜像或 Dockerfile" placeholder="搜索名称、标签或镜像 ID" value={filter} onChange={event => setFilter(event.target.value)} /></div>
      {tab === "images" ? <><div className="docker-table-scroll"><table className="docker-table"><thead><tr><th>镜像 / 标签</th><th>镜像 ID</th><th>大小</th><th>操作</th></tr></thead><tbody>{state?.images.filter(item => [item.id, ...item.tags].some(value => value.toLowerCase().includes(filter.toLowerCase()))).map(item => <tr key={item.id}><td>{item.tags.length ? item.tags.map(tag => <code className="docker-tag" key={tag}>{tag}</code>) : <span className="muted">无标签镜像</span>}</td><td><code title={item.id}>{item.id.slice(0, 23)}…</code></td><td>{(item.size / 1024 ** 2).toFixed(1)} MiB</td><td><button className="text-button" disabled={!item.tags.length || busy || pending} onClick={() => { setError(""); setOperation({ kind: "remove", reference: item.tags[0], image: item }); }}>移除标签</button></td></tr>)}</tbody></table></div>{!state?.images.length && <p className="docker-empty">{state?.images_available ? "本机尚无镜像，可拉取镜像或创建 Dockerfile。" : "等待 Runner 上报镜像列表；表格不会将连接失败显示为镜像已清空。"}</p>}<p className="hint">移除前重新核对镜像 ID、项目配置与所有容器引用。使用中的镜像受保护，操作不强制移除、不清理数据卷。</p></> : <div className="docker-recipe-grid">{state?.recipes.filter(item => item.current && (item.name + item.reference).toLowerCase().includes(filter.toLowerCase())).map(item => <article className="docker-recipe" key={item.id}><div className="section-title"><strong>{item.name}</strong><span className="version-pill">v{item.number}</span></div><code className="docker-tag">{item.reference}</code><pre className="dockerfile-preview">{item.dockerfile}</pre><p className="hint">{item.actor} · {item.reason}</p><div className="button-row"><button className="primary" disabled={busy || pending} onClick={() => { setError(""); setOperation({ kind: "build", reference: item.reference, recipe: item }); }}>构建此版本</button><button className="secondary" onClick={() => { setError(""); setEditor({ recipe: item }); }}>编辑新版</button><button className="text-button" onClick={() => setHistory(item.family)}>版本历史</button></div></article>)}{!state?.recipes.some(item => item.current) && <p className="docker-empty">保存 Dockerfile 后，在这里构建固定版本。</p>}</div>}
    </section>
    <section className="panel"><div className="section-title"><h3>镜像维护记录</h3><button className="text-button" onClick={configureProjects}>配置项目运行镜像 ↗</button></div><p className="hint">拉取与构建完成后不会自动切换已有需求的固定镜像。构建采用仅含 Dockerfile 的上下文；不支持 COPY 本机文件，不上传宿主目录。工作台不保存原始构建输出，本页显示安全的任务结果摘要；失败后可在主机手动排查。</p>{state?.operations.length ? state.operations.map(item => <article className="docker-operation" key={item.id}><div className="section-title"><strong>{imageActions[item.kind] ?? item.kind} · <code>{item.input.reference}</code></strong><span className={`badge state-${item.status.toLowerCase()}`}>{operationStatus[item.status] ?? item.status}</span></div><p className="hint">{timeLabel(item.created_at)} · {item.input.actor} · {item.input.reason}</p>{item.error && <p className="inline-error">{item.error}</p>}{Boolean(item.result) && <details><summary>结果摘要</summary><pre>{JSON.stringify(item.result, null, 2)}</pre></details>}{item.status === "QUEUED" && <button className="text-button" disabled={busy} onClick={() => void cancel(item)}>取消排队</button>}{item.status === "UNKNOWN" && <button className="secondary" onClick={() => { setError(""); setAcknowledge(item); }}>登记人工核实</button>}</article>) : <p className="docker-empty">暂无执行记录。保存模板与真实构建分别记录。</p>}</section>
    {editor && <Modal title={editor.recipe ? "编辑 Dockerfile 新版本" : "新建 Dockerfile"} close={() => setEditor(null)}><RecipeForm initial={editor.recipe} save={async value => { await api("/docker/recipes", value); setEditor(null); setTab("recipes"); await refresh(); }} /></Modal>}
    {operation && <Modal title={imageActions[operation.kind]} close={() => setOperation(null)}><form className="form-stack" onSubmit={submit}><p className="hint">{state?.trial ? "试用站点可查看表单；提交时会提示未连接执行服务，不会产生成功任务。" : "提交后由本机 Runner 执行，结果保存在镜像维护记录。"}</p>{operation.recipe && <p>固定 Dockerfile v{operation.recipe.number} · {operation.recipe.name}</p>}<label>镜像引用<input name="reference" required defaultValue={operation.reference} readOnly={operation.kind === "build"} placeholder="node:22-bookworm-slim" maxLength={240} /></label>{operation.image && <><label>当前镜像 ID<code>{operation.image.id}</code></label><p>仅移除选中的标签。项目配置引用或容器使用中的镜像会被拒绝；不会清理容器或数据卷。</p></>}<label>操作人<input name="actor" required maxLength={160} /></label><label>操作原因<textarea name="reason" required maxLength={4000} /></label>{error && <p className="inline-error" role="alert">{error}</p>}<button className="primary" disabled={busy || pending}>{busy ? "提交中…" : `提交${imageActions[operation.kind]}`}</button></form></Modal>}
    {history && <Modal title="Dockerfile 版本历史" close={() => setHistory(null)}>{state?.recipes.filter(item => item.family === history).sort((a, b) => b.number - a.number).map(item => <article key={item.id}><h3>v{item.number} · {item.reference}</h3><p className="hint">{timeLabel(item.created_at)} · {item.actor} · {item.reason}</p><pre className="dockerfile-preview">{item.dockerfile}</pre></article>)}</Modal>}
    {acknowledge && <Modal title="登记镜像操作人工核实" close={() => setAcknowledge(null)}><form className="form-stack" onSubmit={async event => { event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); try { await api(`/docker/operations/${acknowledge.id}/acknowledge`, { actor: data.get("actor"), reason: data.get("reason"), checked_daemon: data.get("checked") === "on" }); setAcknowledge(null); await refresh(); } catch (failure) { setError((failure as Error).message); } finally { setBusy(false); } }}><p>先在运行 Docker 的主机检查原构建或拉取是否仍在运行、标签实际指向和受影响容器。本操作仅保存人工核实依据，不把原任务认定为成功，也不重放任务。</p><label>核实人<input name="actor" required /></label><label>实际核实依据<textarea name="reason" required /></label><label className="checkbox-label"><input name="checked" type="checkbox" required />已实际检查 Docker，原操作不再执行</label>{error && <p className="inline-error" role="alert">{error}</p>}<button className="primary" disabled={busy}>结束本次核实</button></form></Modal>}
  </main>;
}

function RecipeForm({ initial, save }: { initial: DockerRecipe | null; save: (value: unknown) => Promise<void> }) {
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await save({ expected_recipe: initial?.id ?? null, name: data.get("name"), reference: data.get("reference"), dockerfile: data.get("dockerfile"), actor: data.get("actor"), reason: data.get("reason") }); }
    catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
  }
  return <form className="form-stack" onSubmit={submit}><p className="hint">每次保存创建新版本。保存不会构建镜像。界面构建目标需使用 awb-user/ 前缀及新标签；上下文仅包含此 Dockerfile，不能 COPY 本机文件。不要写入密钥。</p><label>模板名称<input name="name" required defaultValue={initial?.name} maxLength={160} placeholder="例如：Java 21 后端开发环境" autoFocus /></label><label>构建镜像标签<input name="reference" required defaultValue={initial ? initial.reference : "awb-user/backend-dev:v1"} maxLength={240} /></label><label>Dockerfile<textarea aria-label="Dockerfile" name="dockerfile" className="dockerfile-editor" required defaultValue={initial?.dockerfile ?? "FROM agent-workbench-m1:local\nUSER root\n# 在这里安装开发依赖\nUSER node\nWORKDIR /workspace/project\n"} rows={12} maxLength={65536} spellCheck={false} /></label><label>登记人<input name="actor" required maxLength={160} /></label><label>版本原因<textarea name="reason" required maxLength={4000} /></label>{error && <p className="inline-error" role="alert">{error}</p>}<button className="primary" disabled={busy}>{busy ? "保存中…" : "保存 Dockerfile 版本"}</button></form>;
}
