export type EnvironmentEntry = { name: string; value: string };
export type PortMapping = {
  container_port: number;
  host_port: number | null;
  protocol: "tcp" | "udp";
  service: "agent" | "application";
};
export type VolumeMount = {
  source: string;
  target: string;
  read_only: boolean;
};
export type PortRange = { start: number; end: number };

type Props = {
  environment: EnvironmentEntry[];
  setEnvironment: (value: EnvironmentEntry[]) => void;
  ports: PortMapping[];
  setPorts: (value: PortMapping[]) => void;
  volumes: VolumeMount[];
  setVolumes: (value: VolumeMount[]) => void;
  portRange?: PortRange;
  application: boolean;
};

export function RuntimeConfigFields({
  environment,
  setEnvironment,
  ports,
  setPorts,
  volumes,
  setVolumes,
  portRange,
  application,
}: Props) {
  return (
    <>
      <fieldset className="profile-section">
        <legend>环境变量</legend>
        <p className="hint">
          传入开发容器和应用服务。变量值随配置保存，请勿填写工作台自身的控制变量。
        </p>
        {environment.map((item, index) => (
          <div className="config-entry" key={index}>
            <div className="config-pair">
              <label>
                变量名
                <input
                  aria-label={`环境变量 ${index + 1} 名称`}
                  required
                  pattern="[A-Za-z_][A-Za-z0-9_]*"
                  maxLength={64}
                  value={item.name}
                  placeholder="APP_ENV"
                  onChange={(event) =>
                    setEnvironment(
                      environment.map((entry, position) =>
                        position === index
                          ? { ...entry, name: event.target.value }
                          : entry,
                      ),
                    )
                  }
                />
              </label>
              <label>
                变量值
                <input
                  aria-label={`环境变量 ${index + 1} 值`}
                  maxLength={8000}
                  value={item.value}
                  placeholder="development"
                  onChange={(event) =>
                    setEnvironment(
                      environment.map((entry, position) =>
                        position === index
                          ? { ...entry, value: event.target.value }
                          : entry,
                      ),
                    )
                  }
                />
              </label>
            </div>
            <button
              className="text-button"
              type="button"
              onClick={() =>
                setEnvironment(
                  environment.filter((_, position) => position !== index),
                )
              }
            >
              移除环境变量 {index + 1}
            </button>
          </div>
        ))}
        <button
          className="secondary"
          type="button"
          disabled={environment.length >= 64}
          onClick={() =>
            setEnvironment([...environment, { name: "", value: "" }])
          }
        >
          添加环境变量
        </button>
      </fieldset>
      <fieldset className="profile-section">
        <legend>端口映射</legend>
        <p className="hint">
          宿主端口留空时自动分配，只绑定本机 127.0.0.1。
          {portRange
            ? `允许范围：${portRange.start}–${portRange.end}。`
            : "端口必须位于工作台配置的端口范围内。"}
        </p>
        {ports.map((item, index) => (
          <div className="config-entry" key={index}>
            <div className="config-pair">
              <label>
                映射到
                <select
                  aria-label={`端口映射 ${index + 1} 服务`}
                  value={item.service}
                  onChange={(event) =>
                    setPorts(
                      ports.map((entry, position) =>
                        position === index
                          ? {
                              ...entry,
                              service: event.target
                                .value as PortMapping["service"],
                            }
                          : entry,
                      ),
                    )
                  }
                >
                  <option value="agent">开发容器</option>
                  <option value="application">应用服务</option>
                </select>
              </label>
              <label>
                协议
                <select
                  aria-label={`端口映射 ${index + 1} 协议`}
                  value={item.protocol}
                  onChange={(event) =>
                    setPorts(
                      ports.map((entry, position) =>
                        position === index
                          ? {
                              ...entry,
                              protocol: event.target
                                .value as PortMapping["protocol"],
                            }
                          : entry,
                      ),
                    )
                  }
                >
                  <option value="tcp">TCP</option>
                  <option value="udp">UDP</option>
                </select>
              </label>
              <label>
                容器端口
                <input
                  aria-label={`端口映射 ${index + 1} 容器端口`}
                  type="number"
                  required
                  min={1}
                  max={65535}
                  value={item.container_port || ""}
                  onChange={(event) =>
                    setPorts(
                      ports.map((entry, position) =>
                        position === index
                          ? {
                              ...entry,
                              container_port: Number(event.target.value),
                            }
                          : entry,
                      ),
                    )
                  }
                />
              </label>
              <label>
                宿主端口
                <input
                  aria-label={`端口映射 ${index + 1} 宿主端口`}
                  type="number"
                  min={portRange?.start ?? 1}
                  max={portRange?.end ?? 65535}
                  value={item.host_port ?? ""}
                  placeholder="自动分配"
                  onChange={(event) =>
                    setPorts(
                      ports.map((entry, position) =>
                        position === index
                          ? {
                              ...entry,
                              host_port: event.target.value
                                ? Number(event.target.value)
                                : null,
                            }
                          : entry,
                      ),
                    )
                  }
                />
              </label>
            </div>
            {item.service === "application" && !application && (
              <p className="inline-error">
                请在应用服务中勾选启用，或将映射改为开发容器。
              </p>
            )}
            <button
              className="text-button"
              type="button"
              onClick={() =>
                setPorts(ports.filter((_, position) => position !== index))
              }
            >
              移除端口映射 {index + 1}
            </button>
          </div>
        ))}
        <button
          className="secondary"
          type="button"
          disabled={ports.length >= 16}
          onClick={() =>
            setPorts([
              ...ports,
              {
                container_port: 3000,
                host_port: null,
                protocol: "tcp",
                service: "agent",
              },
            ])
          }
        >
          添加端口映射
        </button>
      </fieldset>
      <fieldset className="profile-section">
        <legend>容器挂载</legend>
        <p className="hint">
          直接挂载本机文件或目录到开发和应用容器。读写挂载中的修改会写回宿主机；请选择专用目录。
        </p>
        {volumes.map((item, index) => (
          <div className="config-entry" key={index}>
            <label>
              本机绝对路径
              <input
                aria-label={`容器挂载 ${index + 1} 本机路径`}
                required
                maxLength={2000}
                value={item.source}
                placeholder="例如 D:\\data\\uploads"
                onChange={(event) =>
                  setVolumes(
                    volumes.map((entry, position) =>
                      position === index
                        ? { ...entry, source: event.target.value }
                        : entry,
                    ),
                  )
                }
              />
            </label>
            <div className="config-pair">
              <label>
                容器绝对路径
                <input
                  aria-label={`容器挂载 ${index + 1} 容器路径`}
                  required
                  maxLength={240}
                  value={item.target}
                  placeholder="/mnt/data"
                  onChange={(event) =>
                    setVolumes(
                      volumes.map((entry, position) =>
                        position === index
                          ? { ...entry, target: event.target.value }
                          : entry,
                      ),
                    )
                  }
                />
              </label>
              <label>
                访问权限
                <select
                  aria-label={`容器挂载 ${index + 1} 权限`}
                  value={item.read_only ? "ro" : "rw"}
                  onChange={(event) =>
                    setVolumes(
                      volumes.map((entry, position) =>
                        position === index
                          ? { ...entry, read_only: event.target.value === "ro" }
                          : entry,
                      ),
                    )
                  }
                >
                  <option value="ro">只读</option>
                  <option value="rw">可读写</option>
                </select>
              </label>
            </div>
            <button
              className="text-button"
              type="button"
              onClick={() =>
                setVolumes(volumes.filter((_, position) => position !== index))
              }
            >
              移除容器挂载 {index + 1}
            </button>
          </div>
        ))}
        <button
          className="secondary"
          type="button"
          disabled={volumes.length >= 16}
          onClick={() =>
            setVolumes([
              ...volumes,
              { source: "", target: "/mnt/data", read_only: true },
            ])
          }
        >
          添加容器挂载
        </button>
      </fieldset>
    </>
  );
}
