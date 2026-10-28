import type { Detail } from "./api";
import { RuntimeProfileSync } from "./RuntimeProfileSync";
export function EnvironmentPanel({
  detail,
  disabled,
  action,
}: {
  detail: Detail;
  disabled: boolean;
  action: (kind: string) => Promise<boolean>;
}) {
  const profile = detail.profile;
  const report = detail.preflight;
  const running = detail.workspace.environment === "RUNNING";
  const diagnostic = detail.workspace.environment === "DEBUG_RUNNING";
  return (
    <section className="panel environment-panel">
      <div className="section-title">
        <h2>项目环境与预检</h2>
        {profile && (
          <span className="version-pill">固定配置 v{profile.number}</span>
        )}
      </div>
      <RuntimeProfileSync
        detail={detail}
        disabled={disabled}
        action={() => void action("sync_profile")}
      />
      {profile ? (
        <>
          <p>
            <code>{profile.content.image}</code> · {profile.content.cpus} CPU ·{" "}
            {profile.content.memory_mib} MiB
          </p>
          <p className="hint">
            构建：{profile.content.build_command || "未配置"} · 验证：
            {profile.content.verification_command.trim() || "未配置"}
          </p>
          <p className="hint">
            Agent CLI：{detail.agent_cli?.name ?? "OpenCode（内置）"} · v
            {detail.agent_cli?.number ?? 1}
            {detail.agent_cli?.fixed
              ? "（本需求已固定）"
              : "（创建工作区时固定）"}
            。预检使用该配置中的命令，镜像可仅提供对应的兼容封装程序。
          </p>
          <p className="hint">
            {profile.content.run_as_root
              ? "容器命令以 root 执行，执行前加载 /root/.bashrc。"
              : "容器命令沿用普通用户，执行前加载其可读的 HOME/.bashrc。"}
          </p>
          <div className="button-row">
            {Boolean(profile.content.dependencies?.length) && (
              <div className="dependency-status">
                <strong>
                  独立 PostgreSQL 17 ·{" "}
                  {dependencyLabels[
                    detail.dependency?.phase ?? "NOT_CREATED"
                  ] ?? detail.dependency?.phase}
                </strong>
                <p className="hint">
                  开发环境启动时连接，停止时保存数据。只读分析和评审不接入此数据库。
                </p>
                {detail.dependency?.snapshot && (
                  <p className="hint">
                    最近数据快照：
                    {(detail.dependency.snapshot.size / 1024).toFixed(1)} KiB ·{" "}
                    <code>
                      {detail.dependency.snapshot.sha256.slice(0, 16)}
                    </code>
                  </p>
                )}
                {detail.dependency?.phase === "STOPPED" && (
                  <p className="hint">
                    数据库已停止，数据卷保留；下次启动继续使用。
                  </p>
                )}
                {detail.dependency?.restored_from && (
                  <p className="hint">已从备份快照恢复到本目录的独立数据卷。</p>
                )}
              </div>
            )}
            <button
              className="secondary"
              disabled={disabled || !detail.workspace.snapshot || running || diagnostic}
              onClick={() => void action("preflight")}
            >
              环境预检
            </button>
            <button
              className="secondary"
              disabled={disabled || !running || !profile.content.build_command}
              onClick={() => void action("build")}
            >
              执行项目构建
            </button>
            <button
              className="secondary"
              disabled={disabled || !running}
              onClick={() => void action("verify")}
            >
              执行项目验证
            </button>
          </div>
          <p className="hint">
            先预检，再启动环境。预检检查工具、命令入口和只读挂载，不调用模型、不执行业务测试。项目验证不会自动完成流程评审或材料验收。
          </p>
          {report ? (
            <div className="preflight-result">
              <strong>
                {report.status === "PASSED"
                  ? "环境预检通过"
                  : report.status === "UNKNOWN"
                    ? "预检资源待核实"
                    : "环境预检未通过"}
              </strong>
              <p className="hint">模型：未调用 · 构建/业务测试：未执行</p>
              {report.agent_cli ? (
                <p className="hint">
                  本次检查：{report.agent_cli.name} · v{report.agent_cli.number}
                </p>
              ) : null}
              {report.commands ? (
                <div className="repository-options">
                  <p className="hint">
                    服务命令：<code>{report.commands.serve.join(" ")}</code>
                  </p>
                  <p className="hint">
                    版本命令：<code>{report.commands.version.join(" ")}</code>
                  </p>
                  <p className="hint">
                    检测到的版本：
                    {report.tools?.agent_cli?.value ||
                      report.tools?.opencode?.value ||
                      "尚未取得版本输出"}
                  </p>
                </div>
              ) : null}
              {report.error ? (
                <p className="inline-error" role="alert">
                  {report.error}
                </p>
              ) : null}
              <ul>
                {report.checks.map((item) => (
                  <li key={item.name}>
                    {item.passed ? "✓" : "✕"}{" "}
                    {checkLabels[item.name] ?? item.name}
                  </li>
                ))}
              </ul>
              {report.diagnostics ? (
                <details className="flow-reports">
                  <summary>查看 CLI 启动诊断</summary>
                  <p className="hint">
                    容器状态：{report.diagnostics.container_status ?? "未取得"}{" "}
                    · 退出码：{report.diagnostics.exit_code ?? "未取得"}
                  </p>
                  {report.diagnostics.executable ? (
                    <p className="hint">
                      实际入口程序：<code>{report.diagnostics.executable}</code>
                    </p>
                  ) : null}
                  {report.diagnostics.container_path !== undefined ? (
                    <p className="hint">
                      容器 PATH：
                      <code>
                        {report.diagnostics.container_path || "未设置"}
                      </code>
                    </p>
                  ) : null}
                  {report.diagnostics.container_home ? (
                    <p className="hint">
                      容器 HOME：
                      <code>{report.diagnostics.container_home}</code> · 用户：
                      {report.diagnostics.container_user || "镜像默认"}
                    </p>
                  ) : null}
                  <pre>
                    {[
                      report.diagnostics.launch_error,
                      report.diagnostics.container_error,
                      report.diagnostics.control_error,
                      report.diagnostics.inspection_error,
                      report.diagnostics.log_tail,
                    ]
                      .filter(Boolean)
                      .join("\n") || "暂无更多诊断输出。"}
                  </pre>
                </details>
              ) : null}
              <code className="path">{report.image_id}</code>
            </div>
          ) : (
            <p className="muted">尚未对该工作区执行环境预检。</p>
          )}
        </>
      ) : (
        <p className="muted">
          该需求沿用原有环境。可先停止环境，再同步项目最新运行配置，在现有工作区使用预检能力。
        </p>
      )}
    </section>
  );
}
const checkLabels: Record<string, string> = {
  image_id: "实际镜像一致",
  opencode_version: "Agent CLI 版本符合配置",
  agent_cli_version: "所选 Agent CLI 版本符合配置",
  agent_cli_service: "所选 Agent CLI 兼容控制服务可用",
  superpowers_revision: "Superpowers 版本匹配",
  superpowers_loaded: "Superpowers 技能已加载",
  node_and_git: "Node 与 Git 可用",
  command_entries: "构建与验证命令入口存在",
  config_mounts: "配置文件可读",
  readonly_source: "预检源码只读",
  no_database_access: "预检副本不接入开发数据库",
  source_unchanged: "原工作区代码未变化",
};
const dependencyLabels: Record<string, string> = {
  NOT_CREATED: "未创建",
  INITIALIZING: "初始化中",
  RESTORING: "正在恢复数据",
  RESTORE_PENDING: "恢复未完成，待继续",
  RESTORED: "待启动并恢复数据",
  RUNNING: "上次检查运行中",
  CHECKPOINTING: "正在保存数据",
  STOPPED: "已停止，数据保留",
  UNKNOWN: "数据或资源待核实",
};
