import { useEffect, useState } from "react";
import { labels, timeLabel } from "./api";
import type { ExecutionEvent, Job } from "./api";

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
            ? `${labels[job.action] ?? job.action} · ${labels[job.status] ?? job.status}`
            : "选择一次执行查看日志"}
        </span>
        <span>{connected ? "● 实时连接" : "持久记录 · 最近 500 条"}</span>
      </div>
      <div className="log-output" aria-live="polite" aria-label="执行日志">
        {events.length ? (
          events.map((event) => (
            <div key={event.id} className="log-row">
              <time>{timeLabel(event.created_at)}</time>
              <span>{readable(event)}</span>
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
    </div>
  );
}
