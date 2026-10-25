import type { Detail } from "./api";
import { timeLabel } from "./api";

const phases: Record<string, string> = {
  NOT_STARTED: "尚未启动",
  STARTING: "正在启动",
  READY: "上次 HTTP 检查通过",
  FAILED: "HTTP 检查未通过",
  EXITED: "应用已退出",
  STALE: "代码已变化，需重启服务",
  STOPPED: "服务已停止",
  UNKNOWN: "服务状态待核实",
};

export function ApplicationPanel({
  detail,
  disabled,
  action,
}: {
  detail: Detail;
  disabled: boolean;
  action: (kind: string) => Promise<boolean>;
}) {
  const config = detail.profile?.content.application;
  if (!config) return null;
  const state = detail.application;
  const phase = state?.phase ?? "NOT_STARTED";
  const exists = phase !== "NOT_STARTED" && phase !== "STOPPED";
  const report = state?.last_check;
  return (
    <section className="panel application-panel">
      <div className="section-title">
        <h2>应用服务与 HTTP 检查</h2>
        <span className="version-pill">{phases[phase] ?? phase}</span>
      </div>
      <code className="path">{config.start_command}</code>
      <p className="hint">
        容器端口 {config.port} · GET {config.health_path} · 预期{" "}
        {config.expected_status}
      </p>
      <div className="button-row">
        <button
          className="secondary"
          disabled={
            disabled || exists || detail.workspace.environment !== "RUNNING"
          }
          onClick={() => void action("app_start")}
        >
          启动应用服务
        </button>
        <button
          className="secondary"
          disabled={disabled || !exists}
          onClick={() => void action("app_check")}
        >
          检查 HTTP 响应
        </button>
        <button
          className="secondary"
          disabled={disabled || !exists}
          onClick={() => void action("app_logs")}
        >
          查看应用日志
        </button>
        <button
          className="secondary"
          disabled={disabled || !exists}
          onClick={() => void action("app_stop")}
        >
          停止应用服务
        </button>
      </div>
      <p className="hint">
        每套应用使用独立容器，同样受资源上限约束。停止环境会先停止应用，再保存数据库。HTTP
        检查只确认此路径响应；业务测试、代码评审和材料验收分别完成。
      </p>
      {report && (
        <div className="preflight-result">
          <strong>最近检查：{phases[report.phase] ?? report.phase}</strong>
          <p className="hint">
            {timeLabel(report.checked_at)} ·{" "}
            {report.http?.status
              ? `HTTP ${report.http.status}`
              : "未收到预期响应"}
            {report.http?.latency_ms !== undefined
              ? ` · ${report.http.latency_ms} ms`
              : ""}
          </p>
          <p className="hint">
            检查记录绑定当时的代码；停止后只保留历史，不代表服务仍在线。
          </p>
        </div>
      )}
    </section>
  );
}
