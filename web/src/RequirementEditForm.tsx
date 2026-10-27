import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";

type RequirementContent = {
  title: string;
  description: string;
  acceptance: string;
};

type RequirementRevision = {
  id: string;
  number: number;
  content: RequirementContent;
};

export function RequirementEditForm({
  requirementId,
  onSaved,
}: {
  requirementId: string;
  onSaved: () => Promise<void>;
}) {
  const [revision, setRevision] = useState<RequirementRevision | null>(null);
  const [content, setContent] = useState<RequirementContent>({
    title: "",
    description: "",
    acceptance: "",
  });
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const lifetime = useRef(0);

  useEffect(() => {
    const requested = ++lifetime.current;
    setLoading(true);
    setLoadError("");
    setError("");
    setBusy(false);
    setRevision(null);
    setReason("");
    void api<{ revision: RequirementRevision }>(
      `/requirements/${requirementId}/workflow`,
    )
      .then((value) => {
        if (lifetime.current !== requested) return;
        setRevision(value.revision);
        setContent({ ...value.revision.content });
      })
      .catch((failure) => {
        if (lifetime.current === requested)
          setLoadError((failure as Error).message);
      })
      .finally(() => {
        if (lifetime.current === requested) setLoading(false);
      });
    return () => {
      lifetime.current++;
    };
  }, [requirementId, attempt]);

  const changed = Boolean(
    revision &&
      (content.title.trim() !== revision.content.title ||
        content.description !== revision.content.description ||
        content.acceptance !== revision.content.acceptance),
  );

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!revision || loading || busy || !changed) return;
    const requested = lifetime.current;
    setBusy(true);
    setError("");
    try {
      await api(`/requirements/${requirementId}/revisions`, {
        expected_revision: revision.id,
        title: content.title.trim(),
        description: content.description,
        acceptance: content.acceptance,
        reason: reason.trim() || "修改需求",
      });
      if (lifetime.current !== requested) return;
      await onSaved();
    } catch (failure) {
      if (lifetime.current !== requested) return;
      const message = (failure as Error).message;
      setError(
        message.includes("需求已更新")
          ? "需求已被更新，本次输入已保留。请先复制要保留的修改，再关闭并重新打开编辑窗口，载入最新版本。"
          : message,
      );
    } finally {
      if (lifetime.current === requested) setBusy(false);
    }
  }

  if (loading) {
    return <p role="status">正在加载需求内容…</p>;
  }

  if (loadError || !revision) {
    return (
      <div className="form-stack requirement-edit-form">
        <p className="inline-error" role="alert">
          {loadError || "未能加载需求内容，请重试。"}
        </p>
        <button
          className="secondary"
          type="button"
          onClick={() => setAttempt((value) => value + 1)}
        >
          重新加载
        </button>
      </div>
    );
  }

  return (
    <form className="form-stack requirement-edit-form" onSubmit={save}>
      <p className="hint">
        当前版本 v{revision.number}。保存后生成新版本，保留已有代码与执行记录；相关流程与确认需重新进行。
      </p>
      <label>
        需求名称
        <input
          name="title"
          required
          maxLength={200}
          autoFocus
          value={content.title}
          disabled={busy}
          onChange={(event) =>
            setContent((previous) => ({
              ...previous,
              title: event.target.value,
            }))
          }
        />
      </label>
      <div className="project-form-grid">
        <label>
          需求背景
          <textarea
            name="description"
            rows={7}
            maxLength={32000}
            value={content.description}
            disabled={busy}
            onChange={(event) =>
              setContent((previous) => ({
                ...previous,
                description: event.target.value,
              }))
            }
          />
        </label>
        <label>
          验收标准
          <textarea
            name="acceptance"
            rows={7}
            maxLength={16000}
            value={content.acceptance}
            disabled={busy}
            onChange={(event) =>
              setContent((previous) => ({
                ...previous,
                acceptance: event.target.value,
              }))
            }
          />
        </label>
      </div>
      <label>
        变更原因（可选）
        <input
          name="reason"
          maxLength={4000}
          value={reason}
          disabled={busy}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      <button
        className="primary"
        disabled={busy || !changed || !content.title.trim()}
      >
        {busy ? "保存中…" : "保存修改"}
      </button>
    </form>
  );
}
