import { useEffect, useRef, useState } from "react";
import { api, active, labels, timeLabel } from "./api";
import type { Detail, Job, ModelCatalog } from "./api";

export function WorkflowModelFields({
  detail,
  previous,
  disabled,
  onJob,
  onChanged,
  onReadyChange,
}: {
  detail: Detail;
  previous?: { development_model?: string; review_models: string[] };
  disabled: boolean;
  onJob: (job: Job) => void;
  onChanged: () => Promise<void>;
  onReadyChange: (ready: boolean) => void;
}) {
  const endpoint = `/requirements/${detail.id}`;
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [primary, setPrimary] = useState(previous?.development_model ?? "");
  const [reviewA, setReviewA] = useState(previous?.review_models[0] ?? "");
  const [reviewB, setReviewB] = useState(previous?.review_models[1] ?? "");
  const mounted = useRef(false);

  useEffect(() => {
    let live = true;
    mounted.current = true;
    void api<ModelCatalog>(endpoint + "/models")
      .then((value) => { if (live) setCatalog(value); })
      .catch((failure) => { if (live) setError((failure as Error).message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; mounted.current = false; };
  }, [endpoint]);

  const pending = job ? active(job) : false;
  useEffect(() => {
    if (!job || !active(job)) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const update = async () => {
      try {
        const next = await api<Job>(`/jobs/${job.id}`);
        if (!live) return;
        if (active(next)) {
          setJob(next);
          timer = setTimeout(() => void update(), 1000);
          return;
        }
        setJob(next);
        if (next.status !== "SUCCEEDED") {
          setCatalog({ status: "FAILED", models: [] });
          setError(next.error || "读取模型列表未完成，请检查执行记录后重试。");
          return;
        }
        const result = await api<ModelCatalog>(endpoint + "/models");
        if (live) setCatalog(result);
      } catch (failure) {
        if (live) {
          setError((failure as Error).message);
          timer = setTimeout(() => void update(), 2000);
        }
      }
    };
    void update();
    return () => { live = false; clearTimeout(timer); };
  }, [endpoint, job?.id]);

  const models = catalog?.status === "READY" ? catalog.models : [];
  const contains = (id: string) => models.some((model) => model.id === id);
  const ready = !loading && !pending && contains(primary) && contains(reviewA) && contains(reviewB) && reviewA !== reviewB;
  useEffect(() => { onReadyChange(ready); }, [ready, onReadyChange]);
  const canRead = ["RUNNING", "DEBUG_RUNNING"].includes(detail.workspace.environment);

  async function readModels() {
    setLoading(true);
    setCatalog(null);
    setError("");
    try {
      const next = await api<Job>(endpoint + "/actions", { action: "list_models" });
      if (!mounted.current) return;
      setJob(next);
      onJob(next);
      await onChanged();
    } catch (failure) {
      if (mounted.current) setError((failure as Error).message);
    } finally {
      if (mounted.current) setLoading(false);
    }
  }

  return (
    <fieldset className="form-stack">
      <legend>本轮运行模型</legend>
      <p className="hint">
        从当前固定的 Agent CLI 读取可用模型，不调用模型推理。分析与开发使用同一模型，两个评审模型需不同。
      </p>
      <div className="button-row">
        <button type="button" className="secondary" disabled={disabled || loading || pending || !canRead} onClick={() => void readModels()}>
          {pending ? "正在读取模型…" : catalog?.status === "READY" ? "刷新模型列表" : "读取模型列表"}
        </button>
        {catalog?.checked_at ? <span className="hint">读取于 {timeLabel(catalog.checked_at)}</span> : null}
      </div>
      {!canRead ? <p className="hint">先启动当前需求的 Agent 环境，再读取 CLI 模型列表。</p> : null}
      {loading ? <p className="operation-note" role="status">正在加载模型列表…</p> : null}
      {pending ? <p className="operation-note" role="status">{labels[job!.status]} · 正在执行 Agent CLI 的列出模型命令。</p> : null}
      {error || catalog?.error ? <p className="inline-error" role="alert">{error || catalog?.error}</p> : null}
      {!loading && !pending && catalog?.status === "NOT_LOADED" ? <p className="hint">尚未读取此需求的模型列表。</p> : null}
      {!loading && !pending && catalog?.status === "EMPTY" ? <p className="hint">CLI 没有返回可用模型，请检查 CLI 登录状态或列出模型命令后重新读取。</p> : null}
      {([
        ["分析与开发模型", "development-model", primary, setPrimary],
        ["评审模型 1", "model-a", reviewA, setReviewA],
        ["评审模型 2", "model-b", reviewB, setReviewB],
      ] as const).map(([label, name, value, select]) => (
        <label key={name}>
          {label}
          <select name={name} required value={contains(value) ? value : ""} disabled={disabled || loading || pending || models.length === 0} onChange={(event) => select(event.target.value)}>
            <option value="">请选择模型</option>
            {models.map((model) => <option key={model.id} value={model.id}>{model.label && model.label !== model.id ? `${model.label} · ${model.id}` : model.id}</option>)}
          </select>
        </label>
      ))}
      {contains(reviewA) && reviewA === reviewB ? <p className="inline-error" role="alert">请选择两个不同的评审模型。</p> : null}
    </fieldset>
  );
}
