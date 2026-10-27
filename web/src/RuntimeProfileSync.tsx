import type { Detail } from "./api";

export function RuntimeProfileSync({
  detail,
  disabled,
  action,
}: {
  detail: Detail;
  disabled: boolean;
  action: () => void;
}) {
  const sync = detail.profile_sync;
  const running = detail.workspace.environment === "RUNNING";
  return (
    <div className="environment-sync">
      <div>
        <strong>
          {sync?.latest_profile_number
            ? `项目最新配置 v${sync.latest_profile_number} · Agent CLI v${sync.latest_agent_cli_number}`
            : "项目尚未保存运行配置"}
        </strong>
        <p className="hint">
          {sync?.latest_profile_id
            ? sync.has_update
              ? "项目配置或 Agent CLI 已更新，可同步到当前需求。"
              : "当前已使用最新配置，也可重新同步以更新本机镜像。"
            : "请先在项目登记中保存运行配置。"}
        </p>
      </div>
      <button
        type="button"
        className="secondary"
        disabled={
          disabled ||
          !sync?.latest_profile_id ||
          !detail.workspace.snapshot ||
          running ||
          detail.workspace.environment === "UNKNOWN"
        }
        onClick={action}
      >
        同步最新配置
      </button>
      <p className="hint">
        {running
          ? "请先停止当前环境，再同步配置。"
          : !detail.workspace.snapshot
            ? "请先创建工作区，再同步配置。"
            : "同步完成后，需重新预检再启动环境。"}
        同步保留源码、会话和数据库数据，更新镜像、资源、命令、环境变量、端口、挂载及
        Agent CLI；旧流程与确认需要重新进行。
      </p>
    </div>
  );
}
