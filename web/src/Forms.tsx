import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Project } from "./api";
import { RuntimeImageSelect } from "./RuntimeImageSelect";

export function Modal({
  title,
  children,
  close,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="modal"
      aria-labelledby={id}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <div className="modal-head">
        <h2 id={id}>{title}</h2>
        <button className="icon-button" aria-label="关闭" onClick={close}>
          ×
        </button>
      </div>
      {children}
    </dialog>
  );
}

export function ProjectForm({
  defaultImage = "agent-workbench-m1:local",
  submit,
}: {
  defaultImage?: string;
  submit: (value: {
    name: string;
    source_path: string;
    image: string;
  }) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await submit({
        name: String(form.get("name")).trim(),
        source_path: String(form.get("source_path")).trim(),
        image: String(form.get("image")).trim(),
      });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} className="form-stack">
      <p className="muted">
        登记现有 Git 仓库。需求开发使用独立副本，原始目录作为代码来源。
      </p>
      <label>
        项目名称
        <input
          name="name"
          required
          maxLength={160}
          placeholder="例如：订单服务"
          autoFocus
        />
      </label>
      <label>
        仓库绝对路径
        <input
          name="source_path"
          required
          placeholder="/path/to/backend-repository"
        />
      </label>
      <RuntimeImageSelect initialImage={defaultImage} configuredLabel="系统默认" />
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="primary" disabled={busy}>
        {busy ? "保存中…" : "登记项目"}
      </button>
    </form>
  );
}

export function RequirementForm({
  projects,
  submit,
}: {
  projects: Project[];
  submit: (value: {
    project_id: string;
    title: string;
    description: string;
    acceptance: string;
  }) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await submit({
        project_id: String(form.get("project_id")),
        title: String(form.get("title")).trim(),
        description: String(form.get("description")),
        acceptance: String(form.get("acceptance")),
      });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} className="form-stack">
      <label>
        需求名称
        <input
          name="title"
          required
          maxLength={200}
          placeholder="这次需要完成什么？"
          autoFocus
        />
      </label>
      <label>
        关联项目
        <select name="project_id" required>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        需求背景
        <textarea
          name="description"
          rows={3}
          placeholder="业务背景、改动范围和需要保留的行为"
        />
      </label>
      <label>
        验收标准
        <textarea
          name="acceptance"
          rows={3}
          placeholder="写出可以验证的预期结果"
        />
      </label>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="primary" disabled={busy}>
        {busy ? "创建中…" : "创建需求"}
      </button>
    </form>
  );
}
