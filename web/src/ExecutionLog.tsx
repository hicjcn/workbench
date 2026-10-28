import { useEffect, useState } from "react";
import { api, labels, timeLabel, jobLabel } from "./api";
import type { CommandOutput, ExecutionEvent, Job } from "./api";

function readable(event: ExecutionEvent) {
  const text = event.payload.text ?? event.kind;
  if (event.kind !== "agent") return text;
  try {
    const value = JSON.parse(text);
    if (value.type === "text") return value.part?.text ?? "模型回复";
    if (value.type === "tool_use")
      return `工具 ${value.part?.tool ?? ""}：${value.part?.state?.title ?? value.part?.state?.status ?? "执行中"}`;
    if (value.type === "step_start") return "Agent 开始处理";
    if (value.type === "step_finish") return "Agent 本轮处理结束";
    if (value.type === "error") return JSON.stringify(value.error);
    return `Agent 事件：${value.type}`;
  } catch {
    return text;
  }
}

export function ExecutionLog({ job }: { job: Job | null }) {
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const jobId = job?.id;
  const status = job?.status;
  const currentStage = [...events]
    .reverse()
    .find((event) => event.kind === "stage" && event.payload.text);
  const [verification, setVerification] = useState<{
    id: string;
    output: CommandOutput;
  } | null>(null);
  const [verificationError, setVerificationError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const repositoryResults =
    job?.result?.command_output?.repositories ??
    job?.result?.workflow_output?.repositories ??
    (verification && verification.id === jobId
      ? verification.output.repositories
      : undefined);
  useEffect(() => {
    let live = true;
    setVerificationError("");
    if (!jobId || status === "QUEUED" || status === "RUNNING") return;
    void api<CommandOutput>(`/jobs/${jobId}/verification`)
      .then((output) => {
        if (live) setVerification({ id: jobId, output });
      })
      .catch((failure) => {
        if (live) setVerificationError(failure.message);
      });
    return () => {
      live = false;
    };
  }, [jobId, status, attempt]);
  useEffect(() => {
    setEvents([]);
    setConnected(false);
    if (!jobId) return;
    const source = new EventSource(`/api/v1/jobs/${jobId}/stream`);
    source.onopen = () => setConnected(true);
    source.onmessage = (message) => {
      try {
        const value = JSON.parse(message.data) as ExecutionEvent;
        setEvents((previous) =>
          previous.some((item) => item.id === value.id)
            ? previous
            : [...previous.slice(-499), value],
        );
      } catch {
        setConnected(false);
      }
    };
    source.onerror = () => setConnected(false);
    source.addEventListener("done", () => {
      setConnected(false);
      source.close();
    });
    return () => source.close();
  }, [jobId]);
  return (
    <div className="log-panel">
      <div className="log-caption">
        <span>
          {job
            ? `${jobLabel(job)} · ${labels[job.status] ?? job.status}`
            : "选择一次执行查看日志"}
        </span>
        <span>{connected ? "● 实时连接" : "持久记录 · 最近 500 条"}</span>
      </div>
      {status === "RUNNING" && currentStage ? (
        <p className="hint" role="status">
          当前步骤：{currentStage.payload.text}
        </p>
      ) : null}
      <div className="log-output" aria-live="polite" aria-label="执行日志">
        {events.length ? (
          events.map((event) => (
            <div key={event.id} className="log-row">
              <time>{timeLabel(event.created_at)}</time>
              <span>
                {event.payload.level === "DEBUG" ? "[详细] " : ""}
                {readable(event)}
              </span>
            </div>
          ))
        ) : (
          <p className="log-empty">
            {job
              ? "等待执行器输出，历史记录会自动回放。"
              : "创建工作区或运行任务后，执行过程会显示在这里。"}
          </p>
        )}
      </div>
      {job?.error ? (
        <p className="inline-error" role="alert">
          {job.error}
        </p>
      ) : null}
      {verificationError && (
        <p className="inline-error" role="alert">
          {verificationError}
          <button
            type="button"
            className="text-button"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重试读取记录
          </button>
        </p>
      )}
      {Boolean(repositoryResults?.length) && (
        <div className="repository-options">
          <h3>逐仓库验证记录</h3>
          {repositoryResults!.map((item) => (
            <details key={item.path}>
              <summary>
                <code>{item.path}</code> ·{" "}
                {item.exit_code === 0
                  ? "通过"
                  : item.exit_code !== undefined
                    ? `失败（退出码 ${item.exit_code}）`
                    : item.status === "NOT_RUN"
                      ? "未执行"
                      : "已中断"}
              </summary>
              {item.command && (
                <p className="hint">
                  <code>{item.command.join(" ")}</code>
                </p>
              )}
              {item.output_tail && (
                <pre className="diff">{item.output_tail}</pre>
              )}
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
