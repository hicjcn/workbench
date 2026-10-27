import { useRef, useState } from "react";
import { api, active, labels, jobLabel, timeLabel } from "./api";
import type { Detail, Job } from "./api";
import { ExecutionLog } from "./ExecutionLog";
import { WorkflowPanel } from "./WorkflowPanel";
import type { WorkflowState } from "./WorkflowPanel";
import { MaterialPanel } from "./MaterialPanel";
import type { MaterialState } from "./materialTypes";
import { materialLabels } from "./materialTypes";
import { EnvironmentPanel } from "./EnvironmentPanel";
import { ApplicationPanel } from "./ApplicationPanel";
import { AcceptancePanel } from "./AcceptancePanel";
import {
  currentRequirementStep,
  preparationAction,
  requirementSteps,
} from "./requirementSteps";

function Badge({ value }: { value: string }) {
  return (
    <span className={`badge state-${value.toLowerCase()}`}>
      {labels[value] ?? value}
    </span>
  );
}

export function RequirementDetail({
  detail,
  onChanged,
  configureProject,
}: {
  detail: Detail;
  onChanged: () => Promise<void>;
  configureProject: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [task, setTask] = useState("");
  const [reason, setReason] = useState("");
  const [logId, setLogId] = useState("");
  const [workflow, setWorkflow] = useState<WorkflowState | null>(null);
  const [materials, setMaterials] = useState<MaterialState | null>(null);
  const [workflowError, setWorkflowError] = useState("");
  const [materialError, setMaterialError] = useState("");
  const [selection, setSelection] = useState<{
    index: number;
    current: number;
  } | null>(null);
  const logRef = useRef<HTMLDetailsElement>(null);
  const workflowEnabled = Boolean(workflow?.current);
  async function action(kind: string, prompt = "") {
    setBusy(true);
    setError("");
    try {
      const job = await api<Job>(`/requirements/${detail.id}/actions`, {
        action: kind,
        prompt,
      });
      setLogId(job.id);
      await onChanged();
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
      await onChanged();
      setReason("");
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const running = detail?.jobs.find(active);
  const unknown = detail?.jobs.some((job) => job.status === "UNKNOWN");
  const currentLog =
    detail.jobs.find((job) => job.id === logId) ?? detail.jobs[0] ?? null;
  const disabled =
    busy ||
    !workflow ||
    !materials ||
    Boolean(workflowError || materialError) ||
    Boolean(running) ||
    Boolean(unknown) ||
    detail.workspace.environment === "UNKNOWN";
  const snapshot = detail?.workspace.snapshot;
  const current = currentRequirementStep(detail, workflow, materials);
  const viewed =
    selection?.current === current
      ? Math.min(selection.index, current)
      : current;
  const loaded = Boolean(workflow && materials);
  const blocked = disabled;
  const nextPreparation = preparationAction(detail);
  const phase =
    viewed === 2 ? "development" : viewed === 3 ? "review" : "analysis";
  const needsEnvironment =
    current === 2 && detail.workspace.environment !== "RUNNING";
  const latestFailure = detail.jobs.find((job) =>
    ["FAILED", "CANCELED"].includes(job.status),
  );
  function showLogs() {
    if (logRef.current) logRef.current.open = true;
  }
  return (
    <div className="detail-column guided-detail">
      <section className="panel detail-heading">
        <div className="section-kicker">
          {detail.project.name}
          <span>独立开发</span>
          <span>
            {detail.agent_cli?.name ?? "OpenCode"} · v
            {detail.agent_cli?.number ?? 1}
            {detail.agent_cli?.fixed ? "（已固定）" : "（准备时固定）"}
          </span>
        </div>
        <h2>{detail.title}</h2>
        <details className="requirement-brief">
          <summary>
            查看需求背景与验收标准
            {detail.repositories?.length
              ? ` · ${detail.repositories.length} 个候选仓库`
              : ""}
          </summary>
          <p>{detail.description || "尚未补充需求背景"}</p>
          {detail.acceptance ? (
            <div className="acceptance">
              <strong>验收标准</strong>
              <p>{detail.acceptance}</p>
            </div>
          ) : null}
        </details>
      </section>

      <section className="panel requirement-guide" aria-label="需求推进步骤">
        <div className="section-title">
          <h2>按步骤推进</h2>
          <span className="small-label">
            {loaded
              ? `当前第 ${current + 1} 步 / ${requirementSteps.length}`
              : "正在读取进度…"}
          </span>
        </div>
        <ol className="requirement-steps">
          {requirementSteps.map((step, index) => (
            <li
              key={step.title}
              className={
                index === current
                  ? "current"
                  : index < current
                    ? "completed"
                    : "waiting"
              }
            >
              <button
                type="button"
                disabled={!loaded || index > current}
                aria-current={index === current ? "step" : undefined}
                aria-pressed={viewed === index}
                onClick={() => setSelection({ index, current })}
              >
                <span className="step-number">
                  {index < current ? "✓" : index + 1}
                </span>
                <strong>{step.title}</strong>
                <span>
                  {index < current
                    ? "已完成 · 可回看"
                    : index === current
                      ? "当前步骤"
                      : "等待前置步骤"}
                </span>
              </button>
            </li>
          ))}
        </ol>
        <div className="step-orientation" role="status">
          <div>
            <span className="eyebrow">
              {viewed === current ? "现在要做" : "回看已完成步骤"}
            </span>
            <h3>{requirementSteps[viewed].title}</h3>
            <p>
              {viewed === current
                ? requirementSteps[viewed].description
                : "这里保留本阶段的结果。返回当前步骤，继续处理尚未完成的工作。"}
            </p>
          </div>
          {viewed !== current ? (
            <button className="primary" onClick={() => setSelection(null)}>
              返回当前步骤
            </button>
          ) : loaded && current === 0 ? (
            <button
              className="primary"
              disabled={blocked}
              onClick={() => void action(nextPreparation.action)}
            >
              {nextPreparation.label}
            </button>
          ) : null}
        </div>
      </section>
      {error || workflowError || materialError ? (
        <p role="alert" className="inline-error panel">
          {error || workflowError || materialError}
        </p>
      ) : null}
      {!loaded ? (
        <section className="panel">
          <p className="muted">
            正在核对已保存的流程与材料，读取后显示下一步。
          </p>
        </section>
      ) : null}
      {running ? (
        <section className="panel operation-note" role="status">
          <strong>
            {jobLabel(running)} · {labels[running.status]}
          </strong>
          <p className="hint">完成后会自动更新步骤，无需重复提交。</p>
          <div className="button-row">
            <button className="secondary" onClick={showLogs}>
              查看执行记录
            </button>
            <button
              className="text-button"
              disabled={busy || running.cancel_requested}
              onClick={() =>
                void api(`/jobs/${running.id}/cancel`, {})
                  .then(onChanged)
                  .catch((failure) => setError(failure.message))
              }
            >
              {running.cancel_requested ? "正在停止…" : "取消操作"}
            </button>
          </div>
        </section>
      ) : unknown || detail.workspace.environment === "UNKNOWN" ? (
        <section className="panel recovery-notice" role="alert">
          <h3>先核实运行状态</h3>
          <p>上次操作的状态不明，核实后再继续当前步骤。代码和记录会保留。</p>
          <button
            className="primary"
            disabled={busy}
            onClick={() => void action("reconcile")}
          >
            停止并核实
          </button>
        </section>
      ) : latestFailure && detail.jobs[0]?.id === latestFailure.id ? (
        <section className="panel recovery-notice" role="alert">
          <h3>
            {jobLabel(latestFailure)}
            {latestFailure.status === "CANCELED" ? "已取消" : "未完成"}
          </h3>
          <p>
            {latestFailure.error ||
              "检查执行记录并处理原因后，可在当前步骤重新提交。"}
          </p>
          <button
            className="secondary"
            onClick={() => {
              setLogId(latestFailure.id);
              showLogs();
            }}
          >
            查看失败记录
          </button>
        </section>
      ) : null}
      <div className="step-content" hidden={!loaded || viewed !== 0}>
        <section className="panel preparation-panel">
          <h2>准备独立开发环境</h2>
          <p>
            依次创建代码副本、检查运行配置并启动环境。每一步完成后，上方会显示下一项操作。
          </p>
          <ol className="preparation-checklist">
            <li>
              {snapshot ? "✓" : "○"} 创建独立工作区
              {detail.repositories?.length
                ? `（${detail.repositories.length} 个仓库）`
                : ""}
            </li>
            <li>
              {!detail.profile
                ? "— 沿用项目镜像，无固定配置预检"
                : detail.preflight?.status === "PASSED"
                  ? "✓ 环境预检通过"
                  : "○ 检查运行配置与工具"}
            </li>
            <li>
              {detail.workspace.environment === "RUNNING" ? "✓" : "○"} 启动
              Agent 环境
            </li>
          </ol>
          {current === 0 ? (
            <p className="hint">{nextPreparation.description}</p>
          ) : (
            <p className="operation-note">
              环境准备已完成。停止的环境可通过下方“环境与手动工具”重新启动。
            </p>
          )}
          {!detail.profile ? (
            <button className="text-button" onClick={configureProject}>
              查看项目运行配置
            </button>
          ) : null}
        </section>
      </div>
      {loaded && viewed === current && needsEnvironment ? (
        <section className="panel recovery-notice">
          <h3>先启动开发环境</h3>
          <p>
            开发与验证需要运行中的环境。启动后继续本步，已完成的分析与确认会保留。
          </p>
          <button
            className="primary"
            disabled={blocked}
            onClick={() => void action("start")}
          >
            启动环境并继续
          </button>
        </section>
      ) : null}
      <div
        className="step-content"
        hidden={!loaded || ![1, 2, 3].includes(viewed)}
      >
        <WorkflowPanel
          detail={detail}
          disabled={blocked || (viewed === 2 && needsEnvironment)}
          phase={phase}
          onState={setWorkflow}
          onLoadError={setWorkflowError}
          onJob={(job) => setLogId(job.id)}
          onChanged={onChanged}
        />
      </div>
      <div className="step-content" hidden={!loaded || viewed !== 4}>
        <MaterialPanel
          detail={detail}
          disabled={blocked}
          onState={setMaterials}
          onLoadError={setMaterialError}
          onJob={(job) => setLogId(job.id)}
          onChanged={onChanged}
        />
      </div>
      <div className="step-content" hidden={!loaded || viewed !== 5}>
        <section className="panel">
          <h2>核对证据，准备交付</h2>
          <p>
            设计、主管评审、测试结果与材料验收已登记。请核对完整流程证据，再按团队流程交付。
          </p>
          <p className="hint">
            材料状态：
            {materialLabels[materials?.current?.stage ?? ""] ?? "正在读取"}
            。验收记录不会自动合并代码或发布服务。
          </p>
          <button
            className="secondary"
            onClick={() => setSelection({ index: 4, current })}
          >
            查看材料与测试记录
          </button>
        </section>
        <AcceptancePanel requirementId={detail.id} />
      </div>
      <details
        className="panel detail-drawer"
        key={viewed === 3 ? "review" : "other"}
        open={viewed === 3}
      >
        <summary>代码差异与自审记录</summary>
        <section className="panel">
          <div className="section-title">
            <h2>代码差异与自审</h2>
            <Badge value={detail.workspace.approval} />
          </div>
          {snapshot ? (
            <>
              <div className="snapshot-meta">
                <span>
                  代码快照 <code>{snapshot.tree_digest.slice(0, 12)}</code>
                </span>
                <span>{snapshot.files.length} 个源码文件</span>
              </div>
              {Boolean(snapshot.repositories?.length) && (
                <div className="repository-options">
                  <p className="hint">
                    Agent
                    从共同父目录运行。差异中的路径保留仓库前缀，各仓库分别记录提交与源码摘要。
                  </p>
                  {snapshot.repositories!.map((item) => (
                    <p key={item.path}>
                      <code>{item.path}</code> ·{" "}
                      {item.changed ? "已有改动" : "未改动"} · {item.file_count}{" "}
                      个文件 ·{" "}
                      <code title={item.source_commit}>
                        来源 {item.source_commit.slice(0, 12)}
                      </code>
                    </p>
                  ))}
                </div>
              )}
              <pre className="diff" aria-label="代码差异">
                {detail.workspace.diff
                  ? detail.workspace.diff.split("\n").map((line, index) => (
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
                      disabled={disabled || detail.workspace.diff_truncated}
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
                      <span>{review.reason || "开发者已检查当前快照"}</span>
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
      </details>
      <details className="panel detail-drawer" ref={logRef}>
        <summary>执行记录 · {detail.jobs.length} 次操作</summary>
        <section className="panel">
          <div className="section-title">
            <h2>执行记录</h2>
            <span className="small-label">{detail.jobs.length} 次操作</span>
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
      </details>
      <details className="panel detail-drawer">
        <summary>环境与手动工具</summary>
        <p className="hint">
          日常开发按上方步骤推进。这里可以启停环境、预检、构建、验证与查看应用服务。
        </p>
        {workflowEnabled ? (
          <button
            className="text-button"
            onClick={() => setSelection({ index: 1, current })}
          >
            修订需求或开启新开发轮次
          </button>
        ) : null}
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
              disabled={disabled || Boolean(detail.workspace.snapshot)}
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
          <p className="hint">
            停止环境会保留代码与 Agent 数据。执行状态不明时，先停止并核实。
          </p>
        </section>

        <EnvironmentPanel detail={detail} disabled={disabled} action={action} />
        <ApplicationPanel detail={detail} disabled={disabled} action={action} />
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
      </details>
    </div>
  );
}
