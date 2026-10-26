import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Project } from "./api";
import { RuntimeImageSelect } from "./RuntimeImageSelect";
import { api } from "./api";

type Discovery = {
  source_path: string;
  repositories: { path: string; commit_sha: string }[];
  shared_files: string[];
  limited: boolean;
  max_repositories: number;
};

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
    repositories: string[];
    shared_files: string[];
  }) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [multiple, setMultiple] = useState(false);
  const [source, setSource] = useState("");
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [repositories, setRepositories] = useState<string[]>([]);
  const [shared, setShared] = useState<string[]>([]);
  const [scanning, setScanning] = useState(false);
  function toggle(values: string[], path: string, checked: boolean) {
    return checked ? [...values, path] : values.filter((item) => item !== path);
  }
  async function scan() {
    setScanning(true);
    setError("");
    setDiscovery(null);
    setRepositories([]);
    setShared([]);
    try {
      const value = await api<Discovery>("/projects/discover", {
        source_path: source.trim(),
      });
      setDiscovery(value);
      setRepositories(
        value.repositories
          .slice(0, value.max_repositories)
          .map((item) => item.path),
      );
      setShared(
        value.shared_files.filter((item) =>
          ["AGENTS.md", "README.md"].includes(item),
        ),
      );
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setScanning(false);
    }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    if (multiple && (!discovery || !repositories.length)) {
      setError("请先扫描父目录，并至少选择一个 Git 仓库");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await submit({
        name: String(form.get("name")).trim(),
        source_path: String(form.get("source_path")).trim(),
        image: String(form.get("image")).trim(),
        repositories: multiple ? repositories : [],
        shared_files: multiple ? shared : [],
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
        登记单个 Git 仓库，或登记包含多个 Git
        仓库的业务父目录。需求开发使用独立副本。
      </p>
      <label>
        项目目录类型
        <select
          aria-label="项目目录类型"
          value={multiple ? "multiple" : "single"}
          disabled={scanning}
          onChange={(event) => {
            setMultiple(event.target.value === "multiple");
            setDiscovery(null);
            setRepositories([]);
            setShared([]);
          }}
        >
          <option value="single">单个 Git 仓库</option>
          <option value="multiple">多 Git 仓库父目录</option>
        </select>
      </label>
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
        {multiple ? "父目录绝对路径" : "仓库绝对路径"}
        <input
          name="source_path"
          required
          value={source}
          disabled={scanning}
          onChange={(event) => {
            setSource(event.target.value);
            setDiscovery(null);
            setRepositories([]);
            setShared([]);
          }}
          placeholder={
            multiple
              ? "例如 D:\\code\\business"
              : "例如 D:\\code\\business\\order-service"
          }
        />
      </label>
      <RuntimeImageSelect
        initialImage={defaultImage}
        configuredLabel="系统默认"
      />
      {multiple && (
        <>
          <button
            className="secondary"
            type="button"
            disabled={scanning || !source.trim()}
            onClick={() => void scan()}
          >
            {scanning ? "扫描中…" : "扫描 Git 仓库"}
          </button>
          {discovery && (
            <fieldset className="repository-options">
              <legend>
                项目仓库（已选 {repositories.length}/
                {discovery.max_repositories}）
              </legend>
              {discovery.repositories.map((item) => (
                <label className="checkbox-label" key={item.path}>
                  <input
                    type="checkbox"
                    checked={repositories.includes(item.path)}
                    disabled={
                      !repositories.includes(item.path) &&
                      repositories.length >= discovery.max_repositories
                    }
                    onChange={(event) =>
                      setRepositories(
                        toggle(repositories, item.path, event.target.checked),
                      )
                    }
                  />
                  <span>
                    <code>{item.path}</code> · {item.commit_sha.slice(0, 12)}
                  </span>
                </label>
              ))}
              {!discovery.repositories.length && (
                <p className="hint">
                  没有找到有提交记录的独立 Git 仓库。请核对目录与子仓库状态。
                </p>
              )}
              {discovery.limited && (
                <p className="hint">
                  扫描达到深度或数量限制，可能还有未列出的仓库；可选择更接近这些仓库的父目录。
                </p>
              )}
            </fieldset>
          )}
          {Boolean(discovery?.shared_files.length) && (
            <fieldset className="repository-options">
              <legend>共享说明文件</legend>
              {discovery!.shared_files.map((path) => (
                <label className="checkbox-label" key={path}>
                  <input
                    type="checkbox"
                    checked={shared.includes(path)}
                    onChange={(event) =>
                      setShared(toggle(shared, path, event.target.checked))
                    }
                  />
                  <code>{path}</code>
                </label>
              ))}
            </fieldset>
          )}
          <p className="hint">
            每个需求可再选择候选仓库。独立副本保留相对目录，Agent
            从共同父目录运行；只复制所选仓库和共享文件。
          </p>
        </>
      )}
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <button
        className="primary"
        disabled={
          busy || scanning || (multiple && (!discovery || !repositories.length))
        }
      >
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
    repositories?: string[];
  }) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const selectedProject = projects.find((item) => item.id === projectId);
  const [selectedRepositories, setSelectedRepositories] = useState(
    projects[0]?.repositories ?? [],
  );
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
        repositories: selectedRepositories,
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
        <select
          name="project_id"
          required
          value={projectId}
          onChange={(event) => {
            setProjectId(event.target.value);
            setSelectedRepositories(
              projects.find((item) => item.id === event.target.value)
                ?.repositories ?? [],
            );
          }}
        >
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
      {Boolean(selectedProject?.repositories?.length) && (
        <fieldset className="repository-options">
          <legend>本次需求的候选仓库</legend>
          {selectedProject!.repositories!.map((path) => (
            <label className="checkbox-label" key={path}>
              <input
                type="checkbox"
                checked={selectedRepositories.includes(path)}
                onChange={(event) =>
                  setSelectedRepositories((previous) =>
                    event.target.checked
                      ? [...previous, path]
                      : previous.filter((item) => item !== path),
                  )
                }
              />
              <code>{path}</code>
            </label>
          ))}
          <p className="hint">
            一个业务需求只需录入一次。所选仓库统一复制、分析和开发；创建后集合固定，具体改动范围在分析后确认。
          </p>
        </fieldset>
      )}
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
      <button
        className="primary"
        disabled={
          busy ||
          (Boolean(selectedProject?.repositories?.length) &&
            !selectedRepositories.length)
        }
      >
        {busy ? "创建中…" : "创建需求"}
      </button>
    </form>
  );
}
