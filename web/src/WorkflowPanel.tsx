import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, labels, timeLabel } from "./api";
import type { Detail, Job } from "./api";
import { Modal } from "./Forms";

type Finding = {
  severity: string;
  path: string;
  line: number | null;
  description: string;
  recommendation: string;
};
type Output = {
  summary?: string;
  findings?: Finding[];
  [key: string]: unknown;
};
type Revision = {
  id: string;
  number: number;
  reason: string;
  content: { title: string; description: string; acceptance: string };
};
type Node = {
  key: string;
  label: string;
  model: string | null;
  status: string;
  error: string | null;
  output: Output | null;
  job_id: string | null;
};
type Flow = {
  id: string;
  state: string;
  basis_digest: string;
  next_step: string | null;
  nodes: Node[];
  config: { review_models: string[]; verification_argv: string[] };
  attempts: {
    id: string;
    step_key: string;
    input_digest: string;
    status: string;
    created_at: number;
  }[];
};
type State = {
  revision: Revision;
  revisions: Revision[];
  current: Flow | null;
  history: { id: string; state: string; reason: string; created_at: number }[];
};
type Disposition = { decision: string; reason: string };
const flowLabels: Record<string, string> = {
  ACTIVE: "进行中",
  COMPLETE: "自审完成",
  STALE: "已失效，需新一轮",
  REJECTED: "已退回，需新一轮",
  READY: "可开始",
  PENDING: "等待前置步骤",
  SUPERSEDED: "已被新轮次替代",
};

export function WorkflowPanel({
  detail,
  disabled,
  onJob,
  onEnabled,
  onChanged,
}: {
  detail: Detail;
  disabled: boolean;
  onJob: (job: Job) => void;
  onEnabled: (enabled: boolean) => void;
  onChanged: () => Promise<void>;
}) {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [editing, setEditing] = useState<Revision | null>(null);
  const [archive, setArchive] = useState<
    (Flow & { revision: Revision }) | null
  >(null);
  const [reason, setReason] = useState("");
  const [prompt, setPrompt] = useState("");
  const [dispositions, setDispositions] = useState<Record<string, Disposition>>(
    {},
  );
  const endpoint = `/requirements/${detail.id}`;
  const refresh = useCallback(async () => {
    const next = await api<State>(endpoint + "/workflow");
    setState(next);
    onEnabled(Boolean(next.current));
  }, [endpoint, onEnabled]);
  useEffect(() => {
    let live = true;
    const update = async () => {
      try {
        const next = await api<State>(endpoint + "/workflow");
        if (live) {
          setState(next);
          onEnabled(Boolean(next.current));
        }
      } catch (failure) {
        if (live) setError((failure as Error).message);
      }
    };
    void update();
    const timer = setInterval(() => void update(), 2000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [endpoint, onEnabled]);
  const flow = state?.current;
  const node = flow?.nodes.find((item) => item.key === flow.next_step);
  const gate =
    node && ["scope", "resolution", "self_review"].includes(node.key);
  const blocked = disabled || busy;
  useEffect(() => {
    setReason("");
    setPrompt("");
    setDispositions({});
  }, [flow?.id, flow?.next_step]);

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail.workspace.snapshot) return;
    const values = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await api(endpoint + "/workflow", {
        review_models: [
          String(values.get("model-a")).trim(),
          String(values.get("model-b")).trim(),
        ],
        verification_command: String(values.get("command")).trim(),
        reason: String(values.get("reason")).trim(),
        digest: detail.workspace.snapshot.tree_digest,
      });
      setStarting(false);
      await refresh();
      await onChanged();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function run(decision?: string) {
    if (!flow || !node) return;
    setBusy(true);
    setError("");
    try {
      const job = await api<Job>(endpoint + "/workflow/steps", {
        flow_id: flow.id,
        step_key: node.key,
        prompt,
        reason,
        decision: decision ?? null,
        dispositions,
      });
      onJob(job);
      await refresh();
      await onChanged();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const findings =
    flow?.nodes.flatMap((item) =>
      (item.output?.findings ?? []).map((finding, index) => ({
        ...finding,
        id: `${item.key}:${index}`,
        model: item.model,
      })),
    ) ?? [];

  return (
    <section className="panel workflow-panel">
      <div className="section-title">
        <h2>开发流程</h2>
        <span className="small-label">
          需求 v{state?.revision.number ?? "…"} ·{" "}
          {flow ? flowLabels[flow.state] : "尚未开启"}
        </span>
      </div>
      <p className="hint">
        分析、范围确认、开发、实际验证、多模型评审与自审。每步明确启动，结论绑定需求版本和代码。
      </p>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="button-row">
        <button
          className="secondary"
          disabled={blocked || !detail.workspace.snapshot}
          onClick={() => setStarting((value) => !value)}
        >
          {flow ? "新建流程轮次" : "开启默认流程"}
        </button>
        <button
          className="text-button"
          disabled={blocked || !state}
          onClick={() => setEditing(state!.revision)}
        >
          修订需求
        </button>
      </div>
      {starting ? (
        <form onSubmit={start} className="form-stack flow-start">
          <label>
            评审模型 1
            <input
              name="model-a"
              required
              defaultValue="opencode/glm-5.3-flash-free"
            />
          </label>
          <label>
            评审模型 2
            <input name="model-b" required defaultValue="opencode/big-pickle" />
          </label>
          <label>
            验证命令
            <input
              name="command"
              required
              defaultValue={
                detail.profile?.content.verification_command ?? "npm test"
              }
              maxLength={2000}
            />
          </label>
          <p className="hint">
            命令在需求容器中执行，支持带引号参数；不支持管道或重定向。评审模型需不同且显式免费，凭据由本机
            Runner 配置。
          </p>
          <label>
            本轮原因
            <input
              name="reason"
              required
              defaultValue={flow ? "根据反馈重新分析和开发" : "首次开发"}
              maxLength={4000}
            />
          </label>
          <button className="primary" disabled={blocked}>
            保存并开启本轮
          </button>
        </form>
      ) : null}
      {flow ? (
        <>
          <ol className="flow-timeline" aria-label="默认开发步骤">
            {flow.nodes.map((item, index) => (
              <li
                key={item.key}
                className={flow.next_step === item.key ? "current" : ""}
              >
                <span className="flow-number">
                  {item.status === "SUCCEEDED" ? "✓" : index + 1}
                </span>
                <div>
                  <strong>{item.label}</strong>
                  {item.model ? <code>{item.model}</code> : null}
                </div>
                <span className="flow-status">
                  {flowLabels[item.status] ??
                    labels[item.status] ??
                    item.status}
                </span>
              </li>
            ))}
          </ol>
          <p className="hint">
            本轮代码 <code>{flow.basis_digest.slice(0, 12)}</code> · 验证命令{" "}
            <code>{flow.config.verification_argv.join(" ")}</code>
          </p>
          {findings.length ? (
            <div className="finding-list">
              <h3>评审发现 · {findings.length}</h3>
              {findings.map((finding) => (
                <article className="finding" key={finding.id}>
                  <strong>
                    {finding.severity} · {finding.path}
                    {finding.line ? `:${finding.line}` : ""}
                  </strong>
                  <p>{finding.description}</p>
                  <p className="hint">建议：{finding.recommendation}</p>
                  <code>{finding.model}</code>
                  {node?.key === "resolution" &&
                  ["BLOCKER", "MAJOR"].includes(finding.severity) ? (
                    <div className="form-stack">
                      <label>
                        问题处置 · {finding.id}
                        <select
                          aria-label={`问题处置 · ${finding.id}`}
                          value={dispositions[finding.id]?.decision ?? ""}
                          onChange={(event) =>
                            setDispositions((before) => ({
                              ...before,
                              [finding.id]: {
                                reason: before[finding.id]?.reason ?? "",
                                decision: event.target.value,
                              },
                            }))
                          }
                        >
                          <option value="">请选择</option>
                          <option value="ACCEPTED">接受风险</option>
                          <option value="FALSE_POSITIVE">确认误报</option>
                        </select>
                      </label>
                      <label>
                        处置依据 · {finding.id}
                        <textarea
                          maxLength={4000}
                          value={dispositions[finding.id]?.reason ?? ""}
                          onChange={(event) =>
                            setDispositions((before) => ({
                              ...before,
                              [finding.id]: {
                                decision: before[finding.id]?.decision ?? "",
                                reason: event.target.value,
                              },
                            }))
                          }
                        />
                      </label>
                    </div>
                  ) : null}
                </article>
              ))}
            </div>
          ) : null}
          {node ? (
            <div className="flow-next">
              <h3>下一步：{node.label}</h3>
              {node.error ? (
                <p className="inline-error" role="alert">
                  {node.error}
                </p>
              ) : null}
              {gate ? (
                <>
                  <label className="form-stack">
                    确认依据
                    <textarea
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      maxLength={4000}
                      placeholder="记录你实际检查的内容与决定原因"
                    />
                  </label>
                  <p className="hint">
                    退回会结束本轮；修改代码后重新开启流程并评审。模型报告仅供审查，结论由你确认。
                  </p>
                  <div className="button-row">
                    <button
                      className="primary"
                      disabled={blocked || !reason.trim()}
                      onClick={() => void run("APPROVED")}
                    >
                      确认本步通过
                    </button>
                    <button
                      className="secondary"
                      disabled={blocked || !reason.trim()}
                      onClick={() => void run("REJECTED")}
                    >
                      退回本轮
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <label className="form-stack">
                    本步补充
                    <textarea
                      value={prompt}
                      onChange={(event) => setPrompt(event.target.value)}
                      maxLength={16000}
                      placeholder="可补充关注点，已保存的需求、方案和上游报告会自动带入。"
                    />
                  </label>
                  <button
                    className="primary"
                    disabled={blocked}
                    onClick={() => void run()}
                  >
                    运行：{node.label}
                  </button>
                </>
              )}
            </div>
          ) : (
            <p className="operation-note">
              {flow.state === "COMPLETE"
                ? "本轮分析到自审已完成，可进入后续概要设计阶段。"
                : "本轮结论已结束或失效，历史记录保留；继续开发请新建流程轮次。"}
            </p>
          )}
          <details className="flow-reports">
            <summary>查看本轮报告与执行依据</summary>
            {flow.nodes
              .filter((item) => item.output)
              .map((item) => (
                <div key={item.key}>
                  <h3>{item.label}</h3>
                  <pre>{JSON.stringify(item.output, null, 2)}</pre>
                </div>
              ))}
            {flow.attempts.map((item) => (
              <p className="hint" key={item.id}>
                {item.step_key} · {labels[item.status] ?? item.status} · 输入{" "}
                <code>{item.input_digest.slice(0, 12)}</code> ·{" "}
                {timeLabel(item.created_at)}
              </p>
            ))}
          </details>
        </>
      ) : null}
      {state && state.revisions.length > 1 ? (
        <details className="flow-reports">
          <summary>需求修订历史 · {state.revisions.length}</summary>
          {state.revisions.map((rev) => (
            <div key={rev.id}>
              <h3>
                v{rev.number} · {rev.content.title}
              </h3>
              <p>{rev.reason}</p>
              <p>{rev.content.description}</p>
              <p>{rev.content.acceptance}</p>
            </div>
          ))}
        </details>
      ) : null}
      {state && state.history.length > 1 ? (
        <details className="flow-reports">
          <summary>流程历史 · {state.history.length}</summary>
          {state.history.map((item) => (
            <p key={item.id}>
              {timeLabel(item.created_at)} · {flowLabels[item.state]} ·{" "}
              {item.reason}
              <button
                className="text-button"
                onClick={() =>
                  void api<Flow & { revision: Revision }>(
                    endpoint + "/workflow/runs/" + item.id,
                  )
                    .then(setArchive)
                    .catch((failure) => setError(failure.message))
                }
              >
                查看本轮记录
              </button>
            </p>
          ))}
        </details>
      ) : null}
      {archive ? (
        <Modal title="历史流程记录" close={() => setArchive(null)}>
          <p className="hint">
            需求 v{archive.revision.number} · {archive.revision.content.title} ·
            代码 {archive.basis_digest.slice(0, 12)}
          </p>
          <div className="flow-reports">
            {archive.nodes.map((item) => (
              <div key={item.key}>
                <h3>
                  {item.label} · {labels[item.status] ?? item.status}
                </h3>
                {item.output ? (
                  <pre>{JSON.stringify(item.output, null, 2)}</pre>
                ) : null}
              </div>
            ))}
          </div>
        </Modal>
      ) : null}
      {editing ? (
        <Modal title="修订需求" close={() => setEditing(null)}>
          <RevisionForm
            initial={editing}
            save={async (value) => {
              await api(endpoint + "/revisions", value);
              setEditing(null);
              await refresh();
              await onChanged();
            }}
          />
        </Modal>
      ) : null}
    </section>
  );
}

function RevisionForm({
  initial,
  save,
}: {
  initial: Revision;
  save: (value: unknown) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await save({
        expected_revision: initial.id,
        title: String(values.get("title")),
        description: String(values.get("description")),
        acceptance: String(values.get("acceptance")),
        reason: String(values.get("reason")),
      });
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="form-stack" onSubmit={submit}>
      <p className="hint">
        基于 v{initial.number} 修订。保存后旧流程结论失效，原正文和报告保留。
      </p>
      <label>
        需求名称
        <input
          name="title"
          required
          maxLength={200}
          defaultValue={initial.content.title}
        />
      </label>
      <label>
        需求背景
        <textarea
          name="description"
          aria-label="需求背景"
          maxLength={32000}
          defaultValue={initial.content.description}
        />
      </label>
      <label>
        验收标准
        <textarea
          name="acceptance"
          aria-label="验收标准"
          maxLength={16000}
          defaultValue={initial.content.acceptance}
        />
      </label>
      <label>
        变更原因
        <input name="reason" required maxLength={4000} />
      </label>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="primary" disabled={busy}>
        保存新版本
      </button>
    </form>
  );
}
