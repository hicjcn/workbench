import { useCallback, useEffect, useRef, useState } from "react";
import { api, labels, timeLabel } from "./api";
import type { Detail, Job } from "./api";
import { Modal } from "./Forms";
import { DocumentEditor, EvidenceForm } from "./MaterialForms";
import { materialLabels, materialNames } from "./materialTypes";
import type {
  MaterialRound,
  MaterialState,
  MaterialVersion,
} from "./materialTypes";

const actionNames: Record<string, string> = {
  summary_design: "生成概要设计",
  supervisor_review: "登记主管评审",
  performance_decision: "登记性能测试判断",
  test_plan: "生成功能测试单",
  performance_plan: "生成性能测试单",
  test_execution: "登记测试执行",
  feedback_resolution: "处理未结反馈",
  acceptance: "确认材料阶段验收",
};
const generated = ["summary_design", "test_plan", "performance_plan"];

export function MaterialPanel({
  detail,
  disabled,
  onJob,
  onChanged,
}: {
  detail: Detail;
  disabled: boolean;
  onJob: (job: Job) => void;
  onChanged: () => Promise<void>;
}) {
  const endpoint = `/requirements/${detail.id}/materials`;
  const [data, setData] = useState<MaterialState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingJobId, setPendingJobId] = useState("");
  const pendingRoundId = useRef("");
  const sequence = useRef(0);
  const [modal, setModal] = useState<{
    kind: string;
    state: MaterialRound;
    version?: MaterialVersion;
  } | null>(null);
  const [preview, setPreview] = useState<MaterialVersion | null>(null);
  const [archive, setArchive] = useState<MaterialRound | null>(null);
  const refresh = useCallback(async () => {
    const requested = ++sequence.current;
    const next = await api<MaterialState>(endpoint);
    if (requested === sequence.current) setData(next);
  }, [endpoint]);
  useEffect(() => {
    let live = true;
    const update = async () => {
      const requested = ++sequence.current;
      try {
        const next = await api<MaterialState>(endpoint);
        if (live && requested === sequence.current) setData(next);
      } catch (failure) {
        if (live) setError((failure as Error).message);
      }
    };
    void update();
    const timer = setInterval(() => void update(), 2200);
    return () => {
      live = false;
      sequence.current++;
      clearInterval(timer);
    };
  }, [endpoint]);
  const state = data?.current;
  const waitingForPending = Boolean(
    pendingJobId &&
      pendingRoundId.current === state?.id &&
      !state?.attempts.some(
        (item) =>
          item.job_id === pendingJobId &&
          !["QUEUED", "RUNNING", "UNKNOWN"].includes(item.status),
      ),
  );
  const blocked =
    waitingForPending ||
    disabled ||
    busy ||
    Boolean(
      state?.attempts.some((item) =>
        ["QUEUED", "RUNNING", "UNKNOWN"].includes(item.status),
      ),
    );
  async function start() {
    setBusy(true);
    setError("");
    try {
      const code = await api<{ current: { id: string; basis_digest: string } }>(
        `/requirements/${detail.id}/workflow`,
      );
      await api(endpoint, {
        flow_id: code.current.id,
        digest: code.current.basis_digest,
        reason: state
          ? "重新整理当前已自审代码的材料"
          : "开始概要设计与测试材料",
      });
      setPendingJobId("");
      await refresh();
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function perform(
    kind: string,
    values: Record<string, unknown> = {},
    captured = state,
  ) {
    if (!captured) return;
    setBusy(true);
    setError("");
    try {
      const job = await api<Job>(endpoint + "/steps", {
        round_id: captured.id,
        step_key: kind,
        context_digest: captured.context_digest,
        data: values,
      });
      onJob(job);
      if (captured.id === state?.id) {
        pendingRoundId.current = captured.id;
        setPendingJobId(job.id);
      }
      setModal(null);
      if (captured.id !== state?.id) {
        let completed = false;
        for (let attempt = 0; attempt < 30; attempt++) {
          const observed = await api<Job>(`/jobs/${job.id}`);
          if (!["QUEUED", "RUNNING"].includes(observed.status)) {
            if (observed.status !== "SUCCEEDED")
              throw new Error(
                observed.error || "历史材料导出尚未确认完成，请查看执行记录",
              );
            completed = true;
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        if (!completed)
          throw new Error("历史材料导出仍在处理中，请稍后查看执行记录");
        setArchive(
          (await api<MaterialState>(endpoint + "?round_id=" + captured.id))
            .current,
        );
      }
      await Promise.all([refresh(), onChanged()]);
    } catch (failure) {
      if (modal) throw failure;
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function next() {
    if (!state?.next_step) return;
    const kind =
      state.next_step === "feedback_resolution"
        ? "resolve_feedback"
        : state.next_step;
    if (generated.includes(kind)) void perform(kind);
    else setModal({ kind, state });
  }
  const latestAttempt = state?.attempts.at(-1);
  return (
    <section className="panel material-panel">
      <div className="section-title">
        <h2>设计、测试与反馈</h2>
        <span className="small-label">
          {state ? materialLabels[state.stage] : "等待代码自审"}
        </span>
      </div>
      <p className="hint">
        材料版本、主管意见与实际测试结果分别保存。生成文档后仍需人工评审，生成测试单后仍需实际执行。
      </p>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      {!state || state.stage === "STALE" ? (
        <>
          <p className="muted">
            {state
              ? "本轮材料已过期，原版本与记录保留。当前代码重新自审后可开启新材料轮次。"
              : "完成默认流程自审后，在这里生成概要设计并组织测试材料。"}
          </p>
          <button
            className="secondary"
            disabled={blocked || !data?.can_start}
            onClick={() => void start()}
          >
            {state ? "开启新材料轮次" : "开始材料阶段"}
          </button>
        </>
      ) : (
        <>
          <div className="material-status-grid">
            <div>
              <span>主管评审</span>
              <strong>
                {state.supervisor_valid
                  ? materialLabels[String(state.supervisor?.payload.decision)]
                  : "待登记当前版本"}
              </strong>
            </div>
            <div>
              <span>性能判断</span>
              <strong>{materialLabels[state.performance_decision]}</strong>
            </div>
            <div>
              <span>必需用例</span>
              <strong>{materialLabels[state.test_status]}</strong>
            </div>
            <div>
              <span>未结反馈</span>
              <strong>{state.outstanding_feedback.length}</strong>
            </div>
          </div>
          <div className="material-next">
            <div>
              <strong>{materialLabels[state.stage]}</strong>
              <p className="hint">
                {state.accepted
                  ? "材料阶段验收已登记；全项目交付在后续阶段核实。"
                  : "检查当前版本与依据后，明确启动下一步。"}
              </p>
            </div>
            {state.next_step ? (
              <button className="primary" disabled={blocked} onClick={next}>
                {actionNames[state.next_step]}
              </button>
            ) : null}
          </div>
          {latestAttempt?.status === "FAILED" ||
          latestAttempt?.status === "UNKNOWN" ? (
            <p className="inline-error">{latestAttempt.error}</p>
          ) : null}
        </>
      )}
      {state ? (
        <>
          <p className="hint">
            代码 <code>{state.basis_digest.slice(0, 12)}</code> · 材料轮次{" "}
            <code>{state.id.slice(-8)}</code>
          </p>
          <div className="material-documents">
            {Object.entries(state.documents).map(([kind, version]) => (
              <article className="material-document" key={kind}>
                <div>
                  <strong>{materialNames[kind]}</strong>
                  <span className="small-label">
                    {version
                      ? `v${version.number} · ${materialLabels[version.validity]}`
                      : kind === "performance_plan" &&
                          state.performance_decision === "NOT_REQUIRED"
                        ? "已记录不需要，跳过"
                        : "尚未生成"}
                  </span>
                </div>
                {version ? (
                  <>
                    <p>{version.content.title}</p>
                    <div className="button-row">
                      <button
                        className="text-button"
                        onClick={() => setPreview(version)}
                      >
                        查看{materialNames[kind]}
                      </button>
                      <button
                        className="text-button"
                        disabled={
                          blocked ||
                          !state.context_valid ||
                          (kind !== "summary_design" &&
                            version.validity !== "CURRENT")
                        }
                        onClick={() =>
                          setModal({ kind: "edit_document", state, version })
                        }
                      >
                        编辑{materialNames[kind]}
                      </button>
                      {kind === "summary_design" &&
                      state.context_valid &&
                      state.next_step !== kind ? (
                        <button
                          className="text-button"
                          disabled={blocked}
                          onClick={() => void perform(kind)}
                        >
                          生成概要设计新版
                        </button>
                      ) : null}
                    </div>
                  </>
                ) : null}
              </article>
            ))}
          </div>
          {state.context_valid ? (
            <div className="button-row material-actions">
              <button
                className="secondary"
                disabled={blocked}
                onClick={() => setModal({ kind: "feedback", state })}
              >
                登记反馈
              </button>
              {state.documents.summary_design?.validity === "CURRENT" &&
              state.stage !== "SUPERVISOR_REVIEW" ? (
                <button
                  className="text-button"
                  disabled={blocked}
                  onClick={() => setModal({ kind: "supervisor_review", state })}
                >
                  补充主管记录
                </button>
              ) : null}
              {state.supervisor_valid &&
              state.supervisor?.payload.decision === "APPROVED" &&
              state.stage !== "PERFORMANCE_DECISION" ? (
                <button
                  className="text-button"
                  disabled={blocked}
                  onClick={() =>
                    setModal({ kind: "performance_decision", state })
                  }
                >
                  调整性能判断
                </button>
              ) : null}
            </div>
          ) : null}
          {state.cases.length ? (
            <div className="material-tests">
              <h3>当前测试定义与登记结果</h3>
              {state.cases.map((item) => (
                <div
                  className="material-test-row"
                  key={item.version_id + item.id}
                >
                  <div>
                    <strong>
                      {item.id} · {item.title}
                    </strong>
                    <p className="hint">
                      {materialNames[item.plan_kind]} ·{" "}
                      {item.required ? "必需" : "可选"} · 预期：{item.expected}
                    </p>
                    {item.result ? <p>{item.result.actual}</p> : null}
                  </div>
                  <span className="small-label">
                    {materialLabels[item.status]}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
          <details className="flow-reports">
            <summary>材料版本与记录历史</summary>
            {state.version_history.map((item) => (
              <p key={item.id}>
                {materialNames[item.kind]} v{item.number} ·{" "}
                {materialLabels[item.validity]} ·{" "}
                {item.provenance === "HUMAN" ? "人工修订" : "Agent 生成"}
                <button
                  className="text-button"
                  onClick={() =>
                    void api<MaterialVersion>(endpoint + "/versions/" + item.id)
                      .then(setPreview)
                      .catch((failure) => setError(failure.message))
                  }
                >
                  查看版本
                </button>
              </p>
            ))}
            {state.records.map((item) => (
              <div key={item.id}>
                <strong>
                  {materialNames[item.kind]} · {materialLabels[item.validity]}
                </strong>
                <p className="hint">
                  {timeLabel(item.created_at)} · {String(item.payload.actor)} ·{" "}
                  {String(item.payload.reason)}
                </p>
                <pre>{JSON.stringify(item.payload, null, 2)}</pre>
              </div>
            ))}
          </details>
          <div className="material-export">
            <div className="section-title">
              <h3>材料包</h3>
              <button
                className="secondary"
                disabled={blocked}
                onClick={() => void perform("export")}
              >
                导出当前材料包
              </button>
            </div>
            <p className="hint">
              包含版本、代码摘要、评审、用例及登记结果，按导出时状态保存；证据文件保留引用。
            </p>
            {state.exports.map((item) => (
              <p key={item.id}>
                <a href={`/api/v1${endpoint}/exports/${item.id}`}>
                  下载材料包 · {timeLabel(item.created_at)}
                </a>
                <span className="small-label">
                  {materialLabels[item.manifest.status_at_export] ??
                    item.manifest.status_at_export}
                </span>
                <code>{item.sha256.slice(0, 12)}</code>
              </p>
            ))}
          </div>
        </>
      ) : null}
      {data && data.history.length > 1 ? (
        <details className="flow-reports">
          <summary>材料轮次历史 · {data.history.length}</summary>
          {data.history.map((item) => (
            <p key={item.id}>
              {timeLabel(item.created_at)} · {item.reason}
              <button
                className="text-button"
                onClick={() =>
                  void api<MaterialState>(endpoint + "?round_id=" + item.id)
                    .then((value) => setArchive(value.current))
                    .catch((failure) => setError(failure.message))
                }
              >
                查看材料轮次
              </button>
            </p>
          ))}
        </details>
      ) : null}
      {modal ? (
        <Modal
          title={
            modal.kind === "edit_document"
              ? "修订材料"
              : materialNames[modal.kind]
          }
          close={() => setModal(null)}
        >
          {modal.kind === "edit_document" && modal.version ? (
            <DocumentEditor
              version={modal.version}
              save={(value) => perform("edit_document", value, modal.state)}
            />
          ) : (
            <EvidenceForm
              kind={modal.kind}
              state={modal.state}
              save={(value) => perform(modal.kind, value, modal.state)}
            />
          )}
        </Modal>
      ) : null}
      {preview ? (
        <Modal
          title={`${materialNames[preview.kind]} · v${preview.number}`}
          close={() => setPreview(null)}
        >
          <div className="material-preview">
            <h3>{preview.content.title}</h3>
            <pre>
              {preview.content.markdown ??
                JSON.stringify(preview.content, null, 2)}
            </pre>
            <p className="hint">内容摘要 {preview.content_digest}</p>
          </div>
        </Modal>
      ) : null}
      {archive ? (
        <Modal title="历史材料轮次" close={() => setArchive(null)}>
          <p className="hint">
            代码 {archive.basis_digest.slice(0, 12)} ·{" "}
            {materialLabels[archive.stage]}
          </p>
          {Object.values(archive.documents)
            .filter((item): item is MaterialVersion => Boolean(item))
            .map((item) => (
              <div className="material-preview" key={item.id}>
                <h3>
                  {materialNames[item.kind]} v{item.number}
                </h3>
                <pre>
                  {item.content.markdown ??
                    JSON.stringify(item.content, null, 2)}
                </pre>
              </div>
            ))}
          <button
            className="secondary"
            disabled={blocked}
            onClick={() => void perform("export", {}, archive)}
          >
            导出此历史轮次
          </button>
          {archive.exports.map((item) => (
            <p key={item.id}>
              <a href={`/api/v1${endpoint}/exports/${item.id}`}>
                下载历史材料包 · {timeLabel(item.created_at)}
              </a>
              <span className="small-label">
                {" "}
                ·{" "}
                {materialLabels[item.manifest.status_at_export] ??
                  item.manifest.status_at_export}
              </span>
              <code> · {item.sha256.slice(0, 12)}</code>
            </p>
          ))}
        </Modal>
      ) : null}
    </section>
  );
}
