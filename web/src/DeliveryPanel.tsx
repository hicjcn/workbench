import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, labels, timeLabel } from "./api";
import type { Job, Project, Requirement } from "./api";
import { Modal } from "./Forms";
import { ExecutionLog } from "./ExecutionLog";
import { PanoramaPanel } from "./PanoramaPanel";
import type { PanoramaState } from "./PanoramaPanel";

type Member = {
  requirement_id: string;
  required: boolean;
  skip_reason: string;
};
type Revision = {
  id: string;
  number: number;
  content: { title: string; description: string; members: Member[] };
};
type Group = { id: string; state: string; revision: Revision };
type Decision = {
  id: string;
  payload: { actor: string; reason: string; result?: string; evidence: string };
};
type Delivery = Group & {
  panorama: PanoramaState;
  scope_ready: boolean;
  scope_approved: boolean;
  scope_digest: string;
  baseline_digest: string;
  context_digest: string;
  ready: boolean;
  members_ready: boolean;
  integration_valid: boolean;
  integration: Decision | null;
  scope_decision: Decision | null;
  members: (Member & {
    project: Project;
    requirement: Requirement;
    ready: boolean;
    blocking_reasons: string[];
    baseline: { code_digest: string | null };
    analysis: { summary: string; plan: string } | null;
    material: { stage: string; test_status: string } | null;
  })[];
  exports: {
    id: string;
    sha256: string;
    size: number;
    validity: string;
    created_at: number;
  }[];
  operations: {
    job_id: string;
    kind: string;
    status: string;
    error: string | null;
    created_at: number;
  }[];
  history: Revision[];
};
type Mode =
  | "create"
  | "revise"
  | "scope"
  | "integration"
  | "capture"
  | "archive"
  | "reconcile"
  | "reopen";
const titles: Record<Mode, string> = {
  create: "创建跨项目交付",
  revise: "修订交付范围",
  scope: "确认跨项目范围",
  integration: "登记联调结果",
  capture: "采集完整交付包",
  archive: "归档交付",
  reconcile: "停止全部成员并核实",
  reopen: "重新打开交付",
};

export function DeliveryPanel({
  requirements,
  projects,
  openRequirement,
}: {
  requirements: Requirement[];
  projects: Project[];
  openRequirement: (id: string) => void;
}) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Delivery | null>(null);
  const [form, setForm] = useState<{
    mode: Mode;
    snapshot: Delivery | null;
  } | null>(null);
  const [error, setError] = useState("");
  const [log, setLog] = useState<Job | null>(null);
  const [logId, setLogId] = useState("");
  const [pending, setPending] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let live = true,
      loading = false;
    setDetail(null);
    const update = async () => {
      if (loading) return;
      loading = true;
      try {
        const [listing, next] = await Promise.all([
          api<Group[]>("/deliveries"),
          selected
            ? api<Delivery>(`/deliveries/${selected}`)
            : Promise.resolve(null),
        ]);
        if (!live) return;
        setGroups(listing);
        setDetail(next);
        setPending(false);
        if (!selected && listing.length) setSelected(listing[0].id);
      } catch (failure) {
        if (live) setError((failure as Error).message);
      } finally {
        loading = false;
      }
    };
    void update();
    const timer = setInterval(() => void update(), 1800);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [selected, refresh]);
  useEffect(() => {
    let live = true;
    if (!logId) {
      setLog(null);
      return;
    }
    const update = () =>
      void api<Job>(`/jobs/${logId}`)
        .then((value) => {
          if (live) setLog(value);
        })
        .catch((failure) => {
          if (live) setError(failure.message);
        });
    update();
    const timer = setInterval(update, 1800);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [logId]);
  const running = Boolean(
    detail?.operations.some((item) =>
      ["QUEUED", "RUNNING", "UNKNOWN"].includes(item.status),
    ),
  );
  const disabled = pending || running || !detail;
  const show = (mode: Mode) => setForm({ mode, snapshot: detail });
  async function submit(value: Record<string, unknown>) {
    if (!form) return;
    const mode = form.mode,
      captured = form.snapshot;
    let path = "/deliveries",
      body = value;
    if (mode !== "create") {
      if (!captured) return;
      path += `/${captured.id}`;
      if (mode === "revise") {
        path += "/revisions";
        body = { ...value, expected_revision: captured.revision.id };
      } else if (mode === "scope") {
        path += "/scope";
        body = { ...value, expected_scope: captured.scope_digest };
      } else if (mode === "integration") {
        path += "/integration";
        body = { ...value, expected_baseline: captured.baseline_digest };
      } else if (mode === "reopen") path += "/reopen";
      else {
        path += "/operations";
        body = {
          ...value,
          kind: mode,
          context_digest: captured.context_digest,
        };
      }
    }
    const result = await api<{ id?: string }>(path, body);
    if (mode === "create") setSelected(result.id ?? "");
    else if (["capture", "archive", "reconcile"].includes(mode))
      setLogId(result.id ?? "");
    setPending(true);
    setForm(null);
    setRefresh((previous) => previous + 1);
  }
  async function cancel(jobId: string) {
    setPending(true);
    setError("");
    try {
      await api(`/jobs/${jobId}/cancel`, {});
      setLogId(jobId);
      setRefresh((previous) => previous + 1);
    } catch (failure) {
      setError((failure as Error).message);
      setPending(false);
    }
  }
  return (
    <main className="delivery-space">
      <div className="section-title">
        <div>
          <h2>跨项目交付</h2>
          <p className="muted">
            先汇总分析、确认范围，再分别推进各项目，最后登记联调并统一交付。
          </p>
        </div>
        <button
          className="primary"
          onClick={() => show("create")}
          disabled={!requirements.length}
        >
          ＋ 创建交付
        </button>
      </div>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      <div className="workspace-layout">
        <aside className="requirement-list">
          <div className="list-caption">交付列表 · {groups.length}</div>
          {groups.map((group) => (
            <button
              key={group.id}
              className={`requirement-card ${group.id === selected ? "selected" : ""}`}
              onClick={() => {
                setSelected(group.id);
                setLogId("");
              }}
            >
              <strong>{group.revision.content.title}</strong>
              <span className="muted">
                范围 v{group.revision.number} ·{" "}
                {group.state === "ARCHIVED" ? "已归档" : "进行中"}
              </span>
            </button>
          ))}
        </aside>
        <div className="delivery-content">
          {detail ? (
            <>
              <section className="panel">
                <div className="section-title">
                  <div>
                    <h2>{detail.revision.content.title}</h2>
                    <p className="muted">
                      范围 v{detail.revision.number} ·{" "}
                      {detail.state === "ARCHIVED"
                        ? "已归档"
                        : detail.ready
                          ? "可交付"
                          : "待完成"}
                    </p>
                  </div>
                  <button
                    className="secondary"
                    disabled={disabled || detail.state === "ARCHIVED"}
                    onClick={() => show("revise")}
                  >
                    修订范围
                  </button>
                </div>
                <p>{detail.revision.content.description}</p>
                <div className="delivery-gates">
                  <span>
                    范围 {detail.scope_approved ? "✓ 已确认" : "待确认"}
                  </span>
                  <span>
                    必要项目 {detail.members_ready ? "✓ 已验收" : "待完成"}
                  </span>
                  <span>
                    联调 {detail.integration_valid ? "✓ 已登记" : "待登记"}
                  </span>
                </div>
                <div className="button-row">
                  <button
                    className="secondary"
                    disabled={
                      disabled ||
                      !detail.scope_ready ||
                      detail.state === "ARCHIVED"
                    }
                    onClick={() => show("scope")}
                  >
                    确认范围
                  </button>
                  <button
                    className="secondary"
                    disabled={
                      disabled ||
                      !detail.scope_approved ||
                      !detail.members_ready ||
                      detail.state === "ARCHIVED"
                    }
                    onClick={() => show("integration")}
                  >
                    登记联调
                  </button>
                  <button
                    className="primary"
                    disabled={disabled || !detail.ready}
                    onClick={() => show("capture")}
                  >
                    采集交付包
                  </button>
                  <button
                    className="secondary"
                    disabled={
                      disabled || !detail.ready || detail.state === "ARCHIVED"
                    }
                    onClick={() => show("archive")}
                  >
                    归档交付
                  </button>
                  {detail.state === "ARCHIVED" && (
                    <button
                      className="secondary"
                      disabled={disabled}
                      onClick={() => show("reopen")}
                    >
                      重新打开
                    </button>
                  )}
                  {detail.operations.some(
                    (item) => item.status === "UNKNOWN",
                  ) && (
                    <button
                      className="secondary"
                      disabled={
                        pending ||
                        detail.operations.some((item) =>
                          ["QUEUED", "RUNNING"].includes(item.status),
                        )
                      }
                      onClick={() => show("reconcile")}
                    >
                      停止并核实全部成员
                    </button>
                  )}
                </div>
                {detail.scope_decision && (
                  <p className="hint">
                    范围登记：{detail.scope_decision.payload.actor} ·{" "}
                    {detail.scope_decision.payload.reason}
                  </p>
                )}
                {detail.integration && (
                  <p className="hint">
                    联调登记：{detail.integration.payload.result} ·{" "}
                    {detail.integration.payload.actor} ·{" "}
                    {detail.integration.payload.reason}
                    {!detail.integration_valid && "（未通过或基线已变化）"}
                  </p>
                )}
              </section>
              <PanoramaPanel
                key={detail.id}
                groupId={detail.id}
                state={detail.panorama}
                disabled={disabled}
                archived={detail.state === "ARCHIVED"}
                members={detail.members}
                onJob={setLogId}
                onChanged={() => {
                  setPending(true);
                  setRefresh((previous) => previous + 1);
                }}
              />
              <section className="panel">
                <h3>成员项目 · 按顺序推进</h3>
                {detail.members.map((member, index) => (
                  <article
                    className="delivery-member"
                    key={member.requirement_id}
                  >
                    <div className="section-title">
                      <strong>
                        {index + 1}. {member.project.name} /{" "}
                        {member.requirement.title}
                      </strong>
                      <button
                        className="text-button"
                        onClick={() => openRequirement(member.requirement_id)}
                      >
                        进入需求
                      </button>
                    </div>
                    <p>
                      {member.required
                        ? member.ready
                          ? "必要项目 · 已验收"
                          : `必要项目 · ${member.blocking_reasons.join("；")}`
                        : `本次跳过 · ${member.skip_reason}`}
                    </p>
                    <p className="muted">
                      分析：{member.analysis?.summary ?? "尚未取得当前分析"}
                    </p>
                    {member.analysis && (
                      <p className="hint">计划：{member.analysis.plan}</p>
                    )}
                    <code className="path">
                      {member.baseline.code_digest ?? "工作区尚未准备"}
                    </code>
                  </article>
                ))}
              </section>
              <section className="panel">
                <h3>完整交付包</h3>
                <p className="hint">
                  采集时停止全部成员环境，固定源码、完整差异与材料历史。包内证据保留原始引用。
                </p>
                {detail.exports.length ? (
                  detail.exports.map((item) => (
                    <div key={item.id} className="delivery-member">
                      <a
                        href={`/api/v1/deliveries/${detail.id}/exports/${item.id}`}
                        download
                      >
                        下载交付包 ·{" "}
                        {item.validity === "CURRENT" ? "当前有效" : "历史基线"}
                      </a>
                      <p className="hint">
                        {timeLabel(item.created_at)} ·{" "}
                        {(item.size / 1024).toFixed(1)} KiB
                      </p>
                      <code className="path">SHA-256 {item.sha256}</code>
                    </div>
                  ))
                ) : (
                  <p className="muted">完成所有门禁后可采集。</p>
                )}
              </section>
              <section className="panel">
                <h3>执行与范围历史</h3>
                {detail.operations.map((item) => (
                  <div key={item.job_id}>
                    <button
                      className="requirement-card"
                      onClick={() => setLogId(item.job_id)}
                    >
                      {titles[item.kind as Mode] ??
                        labels[item.kind] ??
                        item.kind}{" "}
                      · {labels[item.status] ?? item.status} ·{" "}
                      {timeLabel(item.created_at)}
                    </button>
                    {["QUEUED", "RUNNING"].includes(item.status) && (
                      <button
                        className="text-button"
                        disabled={pending}
                        onClick={() => void cancel(item.job_id)}
                      >
                        取消执行
                      </button>
                    )}
                  </div>
                ))}
                <ExecutionLog job={log} />
                <details>
                  <summary>范围历史（{detail.history.length}）</summary>
                  {detail.history.map((item) => (
                    <p key={item.id}>
                      v{item.number} · {item.content.title} ·{" "}
                      {item.content.members.length} 个项目
                    </p>
                  ))}
                </details>
              </section>
            </>
          ) : (
            <section className="panel empty-state">
              <h2>
                {selected ? "正在读取交付…" : "将同一需求涉及的项目组织在一起"}
              </h2>
              <p>登记各项目需求后，创建一组交付并明确必要成员及推进顺序。</p>
            </section>
          )}
        </div>
      </div>
      {form && (
        <Modal title={titles[form.mode]} close={() => setForm(null)}>
          <DeliveryForm
            mode={form.mode}
            snapshot={form.snapshot}
            requirements={requirements}
            projects={projects}
            submit={submit}
          />
        </Modal>
      )}
    </main>
  );
}

function DeliveryForm({
  mode,
  snapshot,
  requirements,
  projects,
  submit,
}: {
  mode: Mode;
  snapshot: Delivery | null;
  requirements: Requirement[];
  projects: Project[];
  submit: (value: Record<string, unknown>) => Promise<void>;
}) {
  const editing = mode === "create" || mode === "revise";
  const [members, setMembers] = useState<Member[]>(
    snapshot?.revision.content.members ?? [],
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const value: Record<string, unknown> = Object.fromEntries(data.entries());
    if (editing) value.members = members;
    else value.occurred_at = new Date(String(value.occurred_at)).toISOString();
    setBusy(true);
    setError("");
    try {
      await submit(value);
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function change(index: number, patch: Partial<Member>) {
    setMembers((previous) =>
      previous.map((item, position) =>
        position === index ? { ...item, ...patch } : item,
      ),
    );
  }
  return (
    <form className="form-stack" onSubmit={save}>
      {editing ? (
        <>
          <label>
            交付名称
            <input
              name="title"
              defaultValue={snapshot?.revision.content.title}
              required
              maxLength={200}
            />
          </label>
          <label>
            业务说明
            <textarea
              name="description"
              defaultValue={snapshot?.revision.content.description}
              maxLength={16000}
            />
          </label>
          <fieldset>
            <legend>成员与顺序（每个项目一个需求，最多 8 个）</legend>
            {members.map((member, index) => (
              <div className="delivery-form-member" key={index}>
                <label>
                  第 {index + 1} 个需求
                  <select
                    aria-label={`第 ${index + 1} 个需求`}
                    required
                    value={member.requirement_id}
                    onChange={(event) =>
                      change(index, { requirement_id: event.target.value })
                    }
                  >
                    <option value="">选择需求</option>
                    {requirements.map((item) => (
                      <option key={item.id} value={item.id}>
                        {
                          projects.find(
                            (project) => project.id === item.project_id,
                          )?.name
                        }{" "}
                        / {item.title}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={member.required}
                    onChange={(event) =>
                      change(index, { required: event.target.checked })
                    }
                  />
                  必要成员
                </label>
                {!member.required && (
                  <label>
                    跳过原因
                    <input
                      required
                      value={member.skip_reason}
                      onChange={(event) =>
                        change(index, { skip_reason: event.target.value })
                      }
                    />
                  </label>
                )}
                <button
                  type="button"
                  className="text-button"
                  onClick={() =>
                    setMembers((previous) =>
                      previous.filter((_, position) => position !== index),
                    )
                  }
                >
                  移除此成员
                </button>
              </div>
            ))}
            <button
              type="button"
              className="secondary"
              disabled={members.length >= 8}
              onClick={() =>
                setMembers((previous) => [
                  ...previous,
                  { requirement_id: "", required: true, skip_reason: "" },
                ])
              }
            >
              添加成员
            </button>
          </fieldset>
        </>
      ) : (
        <>
          {mode === "scope" && (
            <label>
              范围决定
              <select name="decision">
                <option value="APPROVED">确认范围</option>
                <option value="REJECTED">退回分析</option>
              </select>
            </label>
          )}
          {mode === "integration" && (
            <>
              <p className="hint">
                登记实际完成的联调结果。工作台不会代替你执行外部业务联调。
              </p>
              <label>
                联调结论
                <select name="result">
                  <option value="PASS">通过</option>
                  <option value="FAIL">失败</option>
                  <option value="NOT_REQUIRED">
                    本次不需要联调（填写原因）
                  </option>
                </select>
              </label>
              <label>
                实际环境
                <input name="environment" required maxLength={4000} />
              </label>
              <label>
                实际结果
                <textarea name="actual" required maxLength={8000} />
              </label>
            </>
          )}
          {mode === "archive" && (
            <p className="hint">
              归档会停止全部成员环境并冻结代码操作。代码、材料和记录将保留。
            </p>
          )}
          {mode === "capture" && (
            <p className="hint">
              采集会停止全部成员环境，并核对磁盘上的实际代码。后续改动需重新采集。
            </p>
          )}
          <label>
            发生时间
            <input
              type="datetime-local"
              name="occurred_at"
              required
              defaultValue={new Date(
                Date.now() - new Date().getTimezoneOffset() * 60000,
              )
                .toISOString()
                .slice(0, 16)}
            />
          </label>
          <label>
            证据或记录位置
            <input
              name="evidence"
              required
              maxLength={4000}
              placeholder="评审记录、测试报告或工单地址"
            />
          </label>
        </>
      )}
      <label>
        登记人
        <input name="actor" required maxLength={160} />
      </label>
      <label>
        {mode === "integration" ? "结论理由" : "操作原因"}
        <textarea name="reason" required maxLength={4000} />
      </label>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      <button
        className="primary"
        disabled={busy || (editing && !members.length)}
      >
        {busy ? "提交中…" : "确认提交"}
      </button>
    </form>
  );
}
