import { useState } from "react";
import type { FormEvent } from "react";
import { api, timeLabel } from "./api";
import { Modal } from "./Forms";

type Entry = {
  id: string;
  kind: string;
  created_at: number;
  code_current: boolean;
  requirements_current: boolean;
  payload: {
    model: string | null;
    model_execution: string;
    actor: string;
    coverage: {
      project_id: string;
      target: string;
      readonly: boolean;
      readable: boolean;
      samples_match: boolean;
    }[];
    report?: {
      summary: string;
      projects: {
        project_id: string;
        impact: string;
        summary: string;
        evidence: {
          path: string;
          line: number | null;
          explanation: string;
          file_sha256: string;
        }[];
      }[];
      relations: {
        from_project: string;
        to_project: string;
        description: string;
      }[];
      risks: string[];
      open_questions: string[];
    };
  };
};
export type PanoramaState = {
  context_digest: string;
  analysis: Entry | null;
  environment: Entry | null;
  history: Entry[];
};
const impacts: Record<string, string> = {
  CHANGE: "建议修改",
  NO_CHANGE: "无需修改",
  UNCERTAIN: "待确认",
};

export function PanoramaPanel({
  groupId,
  state,
  disabled,
  archived,
  members,
  onJob,
  onChanged,
}: {
  groupId: string;
  state: PanoramaState;
  disabled: boolean;
  archived: boolean;
  members: { project: { id: string; name: string } }[];
  onJob: (id: string) => void;
  onChanged: () => void;
}) {
  const [mode, setMode] = useState<"panorama" | "panorama_check" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [captured, setCaptured] = useState("");
  const name = (id: string) =>
    members.find((item) => item.project.id === id)?.project.name ?? id;
  const report = state.analysis?.payload.report;
  function open(next: "panorama" | "panorama_check") {
    setCaptured(state.context_digest);
    setError("");
    setMode(next);
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      const job = await api<{ id: string }>(`/deliveries/${groupId}/panorama`, {
        kind: mode,
        context_digest: captured,
        model: form.get("model") ?? "opencode/glm-5.3-flash-free",
        prompt: form.get("prompt") ?? "",
        actor: form.get("actor"),
        reason: form.get("reason"),
        evidence: "本工作台全景分析运行记录",
        occurred_at: new Date().toISOString(),
      });
      onJob(job.id);
      setMode(null);
      onChanged();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel panorama-panel">
      <div className="section-title">
        <h3>跨项目全景分析</h3>
        <span className="version-pill">{members.length} 个候选项目</span>
      </div>
      <p className="hint">
        同一只读会话查看全部候选项目（含本次跳过的成员），汇总影响、调用关系和风险。请先准备全部工作区，有固定配置时先完成各项目环境预检。运行前会停止全部成员环境并固定代码，保留数据库快照。分析结论不替代各项目分析、自审或人工范围确认。
      </p>
      <div className="button-row">
        <button
          className="secondary"
          disabled={disabled || archived || busy}
          onClick={() => open("panorama_check")}
        >
          检查全景分析环境
        </button>
        <button
          className="secondary"
          disabled={disabled || archived || busy}
          onClick={() => open("panorama")}
        >
          开始联合分析
        </button>
      </div>
      {state.environment && (
        <p className="hint">
          最近环境检查：只读可见性已验证 · 模型未调用 ·{" "}
          {timeLabel(state.environment.created_at)}
          {!state.environment.code_current && " · 基线已变化"}
        </p>
      )}
      {state.analysis && (
        <>
          <p className="hint">
            {state.analysis.payload.model} ·{" "}
            {timeLabel(state.analysis.created_at)} ·{" "}
            {state.analysis.requirements_current
              ? state.analysis.code_current
                ? "当前代码基线"
                : "代码已变化，确认范围前需重新分析"
              : "需求已变化，需重新分析"}
          </p>
          <p>{report?.summary}</p>
          {report?.projects.map((project) => (
            <article className="delivery-member" key={project.project_id}>
              <strong>
                {name(project.project_id)} ·{" "}
                {impacts[project.impact] ?? project.impact}
              </strong>
              <p>{project.summary}</p>
              {project.evidence.map((item, index) => (
                <p className="hint" key={`${item.path}:${index}`}>
                  <code>
                    {item.path}
                    {item.line ? `:${item.line}` : ""}
                  </code>{" "}
                  · {item.explanation} · SHA{" "}
                  {item.file_sha256?.slice(0, 12) ?? "未记录"}
                </p>
              ))}
            </article>
          ))}
          {report?.relations.map((item, index) => (
            <p key={index}>
              {name(item.from_project)} → {name(item.to_project)}：
              {item.description}
            </p>
          ))}
          {Boolean(report?.risks.length) && (
            <>
              <h4>风险</h4>
              <ul>
                {report?.risks.map((risk, index) => (
                  <li key={index}>{risk}</li>
                ))}
              </ul>
            </>
          )}
          {Boolean(report?.open_questions.length) && (
            <>
              <h4>待确认</h4>
              <ul>
                {report?.open_questions.map((question, index) => (
                  <li key={index}>{question}</li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
      {!state.analysis && (
        <p className="muted">
          尚无当前范围的联合分析报告。环境检查通过只证明隔离与可见性。
        </p>
      )}
      {Boolean(state.history.length) && (
        <details>
          <summary>全景历史（{state.history.length}）</summary>
          {state.history.map((item) => (
            <p className="hint" key={item.id}>
              {timeLabel(item.created_at)} ·{" "}
              {item.kind === "PANORAMA" ? "联合分析" : "环境检查"} ·{" "}
              {item.payload.actor} ·{" "}
              {item.payload.report?.summary ?? "模型未调用"}
              {!item.requirements_current && " · 历史需求范围"}
            </p>
          ))}
        </details>
      )}
      {mode && (
        <Modal
          title={
            mode === "panorama" ? "开始跨项目联合分析" : "检查跨项目只读环境"
          }
          close={() => {
            if (!busy) setMode(null);
          }}
        >
          <form className="form-stack" onSubmit={submit}>
            <p className="hint">
              将停止全部候选项目运行环境，再从固定副本创建一个分析容器。
              {mode === "panorama"
                ? "本次会真实调用所选模型，请使用已配置且可访问的免费模型。"
                : "本次不调用模型、不生成影响结论。"}
            </p>
            {mode === "panorama" && (
              <>
                <label>
                  分析模型
                  <input
                    name="model"
                    required
                    maxLength={240}
                    defaultValue="opencode/glm-5.3-flash-free"
                  />
                </label>
                <label>
                  分析补充
                  <textarea name="prompt" maxLength={8000} />
                </label>
              </>
            )}
            <label>
              登记人
              <input name="actor" required maxLength={160} />
            </label>
            <label>
              操作原因
              <textarea name="reason" required maxLength={4000} />
            </label>
            {error && (
              <p role="alert" className="inline-error">
                {error}
              </p>
            )}
            <button className="primary" disabled={busy}>
              {busy ? "提交中…" : "确认提交"}
            </button>
          </form>
        </Modal>
      )}
    </section>
  );
}
