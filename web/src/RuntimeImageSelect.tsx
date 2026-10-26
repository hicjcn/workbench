import { useEffect, useId, useState } from "react";
import { api } from "./api";
import type { DockerState } from "./dockerTypes";

export function RuntimeImageSelect({
  initialImage,
  configuredLabel = "当前配置",
}: {
  initialImage: string;
  configuredLabel?: string;
}) {
  const [image, setImage] = useState(initialImage);
  const [inventory, setInventory] = useState<DockerState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const help = useId();

  useEffect(() => {
    let live = true;
    void api<DockerState>("/docker")
      .then((value) => {
        if (live) setInventory(value);
      })
      .catch((failure) => {
        if (live) setError((failure as Error).message);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [revision]);

  const available = [
    ...new Set(inventory?.images.flatMap((item) =>
      item.tags.length ? item.tags : [item.id],
    ) ?? []),
  ].sort((a, b) => a.localeCompare(b));
  const listed = available.includes(image);
  const fresh = Boolean(
    inventory?.connected && inventory.images_available &&
    !inventory.stale && !inventory.trial && !error && !loading,
  );

  return (
    <>
      <label>
        运行镜像
        <select
          name="image"
          required
          value={image}
          onChange={(event) => setImage(event.target.value)}
          aria-describedby={help}
        >
          {!listed && image && (
            <option value={image}>
              {image}（{image === initialImage ? configuredLabel : "已选镜像"} · {fresh ? "本机未找到" : "待核实"}）
            </option>
          )}
          {!image && <option value="">请选择运行镜像</option>}
          {available.map((reference) => (
            <option value={reference} key={reference}>
              {reference}
            </option>
          ))}
        </select>
      </label>
      <div id={help}>
        <p className="hint">
          镜像需包含所选 Agent CLI、项目依赖及所需技能。支持 OpenCode 或兼容其接口的自定义 CLI，可在“Agent CLI 配置”中管理；项目可在运行配置中选择 CLI。
        </p>
        <p className="hint" role="status">
          {loading
            ? "正在加载 Runner 的本机镜像列表…"
            : inventory?.trial
              ? "当前为示例镜像列表，未连接本机 Docker。"
              : !fresh
                ? "镜像列表暂未核实，保留已选值；请确认 Runner 和 Docker 已启动后刷新。"
                : !available.length
                  ? "本机尚无镜像。请先在“Docker 镜像”页构建或拉取镜像，再刷新列表。"
                  : !listed
                    ? "已选镜像在本机未找到。请改选已有镜像，或先在“Docker 镜像”页构建或拉取后刷新。"
                    : "列表来自 Runner 上报的本机 Docker；构建或拉取镜像后可刷新。"}
        </p>
      </div>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <button
        type="button"
        className="text-button"
        disabled={loading}
        onClick={() => {
          setLoading(true);
          setError("");
          setRevision((value) => value + 1);
        }}
      >
        {loading ? "加载镜像中…" : "刷新镜像列表"}
      </button>
    </>
  );
}
