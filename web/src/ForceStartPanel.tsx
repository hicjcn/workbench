import type { Detail } from "./api";

export function ForceStartPanel({
  detail,
  disabled,
  action,
}: {
  detail: Detail;
  disabled: boolean;
  action: () => void;
}) {
  const diagnostic = detail.workspace.environment === "DEBUG_RUNNING";
  const runtime = diagnostic ? detail.debug_runtime : null;
  return (
    <div className="environment-sync">
      <div>
        <strong>环境启动排障</strong>
        <p className="hint">
          CLI 登录或预检失败时，可跳过预检，按当前固定配置启动容器，检查凭据挂载、环境变量和运行用户。保留镜像默认入口与命令；工作台跳过自动启动 Agent CLI。
        </p>
      </div>
      <button
        type="button"
        className="secondary"
        disabled={
          disabled ||
          !detail.workspace.snapshot ||
          ["RUNNING", "DEBUG_RUNNING", "UNKNOWN"].includes(detail.workspace.environment)
        }
        onClick={action}
      >
        强制启动环境（排障）
      </button>
      {diagnostic ? (
        <div role="status">
          <p className="operation-note">
            排障容器已启动，工作台未启动 Agent CLI。镜像默认入口可能自行启动服务。检查完成后先停止环境，再重新预检并正常启动。
          </p>
          {runtime ? (
            <>
              <p className="hint">容器：<code>{runtime.container}</code></p>
              <p className="hint">在 Runner 主机终端进入容器：</p>
              <code className="path">{runtime.shell_command}</code>
              <p className="hint">
                工作台为 Agent 设置独立 HOME：<code>/home/node</code>。请按此目录核对凭据挂载位置。
              </p>
              {detail.profile?.content.run_as_root ? (
                <>
                  <p className="hint">当前以 root 运行，先加载与工作台相同的初始化文件；该命令执行后 HOME 仍为 /home/node：</p>
                  <code className="path">HOME=/root source /root/.bashrc</code>
                </>
              ) : null}
              <p className="hint">在容器内运行本需求固定的 CLI 服务命令：</p>
              <code className="path">{runtime.serve_command}</code>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
