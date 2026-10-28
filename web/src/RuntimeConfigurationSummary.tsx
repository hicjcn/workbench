import type { Detail } from "./api";

export function RuntimeConfigurationSummary({ detail }: { detail: Detail }) {
  const content = detail.profile?.content;
  const published = [
    ...(detail.workspace.environment === "RUNNING"
      ? (detail.port_mappings ?? [])
      : []),
    ...(detail.application &&
    !["STOPPED", "UNKNOWN"].includes(detail.application.phase)
      ? (detail.application.port_mappings ?? [])
      : []),
  ];
  return (
    <>
      <div>
        <span>运行用户与初始化</span>
        <code>
          {content?.run_as_root
            ? "root · source /root/.bashrc"
            : "普通用户 · HOME/.bashrc（可读时）"}
        </code>
      </div>
      <div>
        <span>环境变量</span>
        <code>
          {Object.keys(content?.environment ?? {}).join("、") || "未配置"}
        </code>
      </div>
      <div>
        <span>容器挂载</span>
        {content?.volume_mounts?.length ? (
          content.volume_mounts.map((mount) => (
            <code key={mount.target}>
              {mount.source} → {mount.target}（
              {mount.read_only ? "只读" : "可读写"}）
            </code>
          ))
        ) : (
          <code>无额外挂载</code>
        )}
      </div>
      <div>
        <span>端口映射配置</span>
        {content?.port_mappings?.length ? (
          content.port_mappings.map((mapping) => (
            <code
              key={`${mapping.service}-${mapping.container_port}-${mapping.protocol}`}
            >
              {mapping.service === "application" ? "应用" : "开发"}：
              {mapping.host_port ?? "自动分配"} → {mapping.container_port}/
              {mapping.protocol}
            </code>
          ))
        ) : (
          <code>未配置</code>
        )}
      </div>
      <div>
        <span>已发布的本机端口</span>
        {published.length ? (
          published.map((mapping) => (
            <code key={`${mapping.host_port}-${mapping.protocol}`}>
              127.0.0.1:{mapping.host_port} → {mapping.container_port}/
              {mapping.protocol}
            </code>
          ))
        ) : (
          <code>启动对应容器后显示</code>
        )}
      </div>
    </>
  );
}
