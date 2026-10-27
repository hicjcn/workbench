import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, timeLabel } from "./api";
import { Modal } from "./Forms";

export type AgentCliContent = { protocol: "opencode"; serve_command: string; run_command: string; version_command: string; expected_version: string; timeout_seconds: number; credential_env: string[] };
export type AgentCliConfig = { id: string; family: string; number: number; name: string; content: AgentCliContent; content_digest: string; actor: string; reason: string; created_at: number; builtin?: boolean; current?: boolean; fixed?: boolean };
export type AgentCliState = { default_id: string; configs: AgentCliConfig[] };
export const wrapperDefaults: AgentCliContent = { protocol: "opencode", serve_command: "my-opencode serve --hostname {host} --port {port}", run_command: "my-opencode run --attach {endpoint} --model {model} --format json", version_command: "my-opencode --version", expected_version: "", timeout_seconds: 300, credential_env: [] };

export function AgentCliPanel({ configureProjects, openContainers }: { configureProjects: () => void; openContainers: () => void }) {
  const [state, setState] = useState<AgentCliState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ config: AgentCliConfig | null } | null>(null);
  const [history, setHistory] = useState<string | null>(null);
  const refresh = useCallback(async () => setState(await api<AgentCliState>("/agent-clis")), []);
  useEffect(() => {
    let live = true;
    const update = () => void api<AgentCliState>("/agent-clis").then(value => { if (live) setState(value); }).catch(failure => { if (live) setError(failure.message); });
    update(); const timer = setInterval(update, 4000); return () => { live = false; clearInterval(timer); };
  }, []);
  async function activate(item: AgentCliConfig) {
    setBusy(true); setError("");
    try { await api(`/agent-clis/${item.id}/default`, {}); await refresh(); }
    catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
  }
  const chosen = state?.configs.find(item => item.id === state.default_id);
  return <main className="docker-space">
    <div className="section-title docker-heading"><div><p className="eyebrow">AGENT / CLI</p><h2>Agent CLI 配置</h2><p className="muted">在 OpenCode 与你的兼容封装 CLI 之间切换，集中维护命令、超时和配置版本。</p></div><button className="primary" onClick={() => setEditor({ config: null })}>＋ 添加封装 CLI</button></div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <section className="panel"><div className="section-title"><div><h3>新工作区默认 CLI</h3><strong>{chosen ? `${chosen.name} · v${chosen.number}` : "正在读取配置…"}</strong></div><span className="version-pill">OpenCode 兼容协议</span></div><p className="hint">默认选择用于新准备的工作区，项目可选择具体版本覆盖默认值。已有需求可停止环境后，在“项目环境与预检”中同步；历史执行保留原版本。</p><div className="button-row"><button className="secondary" onClick={configureProjects}>为项目选择 CLI</button><button className="secondary" onClick={openContainers}>到容器管理执行预检</button></div></section>
    <div className="docker-recipe-grid">{state?.configs.filter(item => item.current || item.id === state.default_id).map(item => <article className="panel agent-cli-card" key={item.id}><div className="section-title"><h3>{item.name}</h3><span className="version-pill">{item.id === state.default_id ? "当前默认 · " : ""}v{item.number}</span></div><p className="small-label">{item.builtin ? "内置 OpenCode" : "自定义兼容封装"} · {item.content.timeout_seconds} 秒任务超时</p><dl className="agent-cli-commands"><dt>启动服务</dt><dd><code>{item.content.serve_command}</code></dd><dt>执行任务</dt><dd><code>{item.content.run_command}</code></dd><dt>版本检查</dt><dd><code>{item.content.version_command}</code></dd><dt>凭据变量名</dt><dd><code>{item.content.credential_env.join("、") || "沿用已配置模型认证，无额外变量"}</code></dd></dl><p className="hint">{item.reason}</p><div className="button-row"><button className="primary" disabled={busy || item.id === state.default_id} onClick={() => void activate(item)}>{item.id === state.default_id ? "已为默认" : "设为新工作区默认"}</button><button className="secondary" onClick={() => setEditor({ config: item })}>{item.builtin ? "复制为封装配置" : "编辑新版本"}</button>{!item.builtin && <button className="text-button" onClick={() => setHistory(item.family)}>版本历史</button>}</div></article>)}</div>
    <section className="panel"><h3>接入步骤与兼容要求</h3><ol className="agent-cli-guide"><li>在「Docker 镜像」准备已安装封装 CLI 的镜像，程序路径必须能在该容器中执行。</li><li>添加配置。启动命令需提供 OpenCode 兼容 HTTP 服务，使用指定端口和 OpenCode 服务认证环境变量；任务命令通过标准输入接收提示词，输出 OpenCode JSON 行事件和明确结束事件。</li><li>选择默认值或在「项目运行配置」固定版本，再创建新工作区或同步已有工作区并进行环境预检。预检通过只说明工具及接口可用，仍需实际任务验证模型和封装逻辑。</li></ol><p className="hint">命令只在隔离容器内执行，不执行宿主 Shell。端口固定为 4096；{`{host}、{port}、{endpoint}、{model}`} 由执行器替换。凭据只填写 Runner 环境变量名，不在页面保存密钥值。只读评审使用独立副本并保持只读源码挂载。</p></section>
    {editor && <Modal title={editor.config && !editor.config.builtin ? "编辑 Agent CLI 新版本" : "添加 OpenCode 兼容封装 CLI"} close={() => setEditor(null)}><AgentCliForm initial={editor.config} save={async value => { await api("/agent-clis", value); setEditor(null); await refresh(); }} /></Modal>}
    {history && <Modal title="Agent CLI 版本历史" close={() => setHistory(null)}>{state?.configs.filter(item => item.family === history).sort((a, b) => b.number - a.number).map(item => <article className="docker-operation" key={item.id}><h3>{item.name} · v{item.number}</h3><p className="hint">{timeLabel(item.created_at)} · {item.actor} · {item.reason}</p><pre className="dockerfile-preview">{JSON.stringify(item.content, null, 2)}</pre><button className="secondary" disabled={busy || item.id === state.default_id} onClick={() => void activate(item)}>将此版本设为新工作区默认</button></article>)}</Modal>}
  </main>;
}

function AgentCliForm({ initial, save }: { initial: AgentCliConfig | null; save: (value: unknown) => Promise<void> }) {
  const defaults = initial?.content ?? wrapperDefaults;
  const [serve, setServe] = useState(defaults.serve_command);
  const [run, setRun] = useState(defaults.run_command);
  const [version, setVersion] = useState(defaults.version_command);
  const [executable, setExecutable] = useState(defaults.serve_command.split(" ")[0]);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  function program(value: string) { setExecutable(value); setServe(`${value} serve --hostname {host} --port {port}`); setRun(`${value} run --attach {endpoint} --model {model} --format json`); setVersion(`${value} --version`); }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await save({ expected_config: initial?.id ?? null, name: data.get("name"), actor: data.get("actor"), reason: data.get("reason"), set_default: data.get("set_default") === "on", content: { protocol: "opencode", serve_command: serve, run_command: run, version_command: version, expected_version: data.get("expected_version"), timeout_seconds: Number(data.get("timeout")), credential_env: String(data.get("credentials") ?? "").split(/[,，\s]+/).filter(Boolean) } }); }
    catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
  }
  return <form className="form-stack" onSubmit={submit}><p className="hint">用于基于 OpenCode 封装、保留兼容服务与 JSON 事件的 CLI。保存只登记配置，不安装工具、不启动任务。示例名称 my-opencode 需替换为你的实际程序。</p><label>配置名称<input name="name" required maxLength={160} defaultValue={initial?.builtin ? "我的 OpenCode 封装" : initial?.name} placeholder="例如：团队开发 CLI" autoFocus /></label><label>容器内 CLI 程序<input value={executable} onChange={event => program(event.target.value)} required placeholder="例如：/usr/local/bin/team-code" /></label><p className="hint">修改程序名会重填下方三条命令；如果封装改变了子命令或参数，可逐项编辑。</p><label>服务启动命令<textarea aria-label="服务启动命令" value={serve} onChange={event => setServe(event.target.value)} required rows={2} maxLength={4000} spellCheck={false} /></label><label>任务执行命令<textarea aria-label="任务执行命令" value={run} onChange={event => setRun(event.target.value)} required rows={2} maxLength={4000} spellCheck={false} /></label><label>版本检查命令<input aria-label="版本检查命令" value={version} onChange={event => setVersion(event.target.value)} required maxLength={2000} /></label><label>预期版本输出<input name="expected_version" defaultValue={initial?.builtin ? "" : defaults.expected_version} maxLength={200} placeholder="可选；填写时须与版本命令输出完全一致" /></label><label>任务超时（秒）<input name="timeout" type="number" min={1} max={3600} required defaultValue={defaults.timeout_seconds} /></label><label>凭据与封装配置变量名<input name="credentials" defaultValue={defaults.credential_env.join(", ")} placeholder="例如：TEAM_API_KEY, AGENT_CONFIG" maxLength={1200} /></label><p className="hint">仅填写变量名，由 Runner 从现有环境按名称注入，不保存实际值。支持 API_KEY、TOKEN、BASE_URL 后缀或 AGENT_ 前缀；系统和工作台变量不可传递。</p><label>登记人<input name="actor" required maxLength={160} /></label><label>变更原因<textarea name="reason" required maxLength={4000} /></label><label className="checkbox-label"><input name="set_default" type="checkbox" />同时设为新工作区默认 CLI</label>{error && <p className="inline-error" role="alert">{error}</p>}<button className="primary" disabled={busy}>{busy ? "保存中…" : "保存 CLI 配置版本"}</button></form>;
}
