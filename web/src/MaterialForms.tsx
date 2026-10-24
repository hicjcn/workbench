import { useState } from "react";
import type { FormEvent } from "react";
import type {
  MaterialContent,
  MaterialRound,
  MaterialVersion,
} from "./materialTypes";
import { materialLabels } from "./materialTypes";

function localNow() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 19);
}

export function EvidenceForm({
  kind,
  state,
  save,
}: {
  kind: string;
  state: MaterialRound;
  save: (data: Record<string, unknown>) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedbackKind, setFeedbackKind] = useState("BUG");
  const [results, setResults] = useState(() =>
    state.cases.map((item) => ({
      ...item,
      selected: false,
      actual: "",
      evidence: "",
      status: "NOT_RUN",
    })),
  );
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const data: Record<string, unknown> = {
      actor: String(values.get("actor")),
      occurred_at: new Date(String(values.get("occurred_at"))).toISOString(),
      reason: String(values.get("reason")),
      evidence: String(values.get("evidence")),
    };
    if (
      [
        "supervisor_review",
        "performance_decision",
        "resolve_feedback",
      ].includes(kind)
    )
      data.decision = String(values.get("decision"));
    if (kind === "test_execution") {
      data.environment = String(values.get("environment"));
      data.results = results
        .filter((item) => item.selected)
        .map((item) => ({
          version_id: item.version_id,
          case_id: item.id,
          status: item.status,
          actual: item.actual,
          evidence: item.evidence,
        }));
      if (!(data.results as unknown[]).length) {
        setError("请至少选择一个本次登记的用例");
        return;
      }
    }
    if (kind === "resolve_feedback")
      data.feedback_id = String(values.get("feedback_id"));
    if (kind === "feedback") {
      data.kind = feedbackKind;
      data.scope_unchanged =
        feedbackKind === "BUG" && values.get("scope_unchanged") === "on";
      data.new_requirement =
        feedbackKind === "REQUIREMENT_CHANGE"
          ? {
              title: String(values.get("title")),
              description: String(values.get("description")),
              acceptance: String(values.get("acceptance")),
            }
          : null;
    }
    setBusy(true);
    setError("");
    try {
      await save(data);
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="form-stack" onSubmit={submit}>
      <p className="hint">
        记录绑定此表单打开时的材料版本。外部证据保存引用，不会自动读取或发送消息。
      </p>
      {kind === "supervisor_review" ? (
        <label>
          主管结论
          <select name="decision" aria-label="主管结论" defaultValue="APPROVED">
            <option value="APPROVED">通过</option>
            <option value="REJECTED">退回</option>
          </select>
        </label>
      ) : null}
      {kind === "performance_decision" ? (
        <label>
          是否需要性能测试
          <select
            name="decision"
            aria-label="是否需要性能测试"
            defaultValue="UNDECIDED"
          >
            <option value="UNDECIDED">待明确</option>
            <option value="REQUIRED">需要</option>
            <option value="NOT_REQUIRED">不需要，记录原因</option>
          </select>
        </label>
      ) : null}
      {kind === "feedback" ? (
        <>
          <label>
            反馈类型
            <select
              aria-label="反馈类型"
              value={feedbackKind}
              onChange={(event) => setFeedbackKind(event.target.value)}
            >
              <option value="BUG">Bug</option>
              <option value="REQUIREMENT_CHANGE">需求变更</option>
              <option value="ARCHITECTURE_CHANGE">架构变更</option>
              <option value="DOCUMENT_COMMENT">文档意见</option>
            </select>
          </label>
          {feedbackKind === "BUG" ? (
            <label className="checkbox-label">
              <input type="checkbox" name="scope_unchanged" />
              修复沿用原已确认范围与方案，直接进入开发
            </label>
          ) : null}
          {feedbackKind === "REQUIREMENT_CHANGE" ? (
            <>
              <label>
                新需求名称
                <input name="title" required maxLength={200} />
              </label>
              <label>
                新需求背景
                <textarea name="description" maxLength={32000} />
              </label>
              <label>
                新验收标准
                <textarea name="acceptance" maxLength={16000} />
              </label>
            </>
          ) : null}
          <p className="hint">
            Bug
            开启修复轮次；需求及架构变更创建新版需求并返回分析；文档意见要求更新文档并重新评审。不会自动启动
            Agent。
          </p>
        </>
      ) : null}
      {kind === "resolve_feedback" ? (
        <>
          <label>
            待处理反馈
            <select aria-label="待处理反馈" name="feedback_id">
              {state.outstanding_feedback.map((item) => (
                <option key={item.id} value={item.id}>
                  {materialLabels[String(item.payload.kind)]} ·{" "}
                  {String(item.payload.reason)}
                </option>
              ))}
            </select>
          </label>
          <label>
            处理结论
            <select name="decision" aria-label="处理结论">
              <option value="RESOLVED">已解决</option>
              <option value="ACCEPTED">接受并记录风险</option>
            </select>
          </label>
        </>
      ) : null}
      <label>
        {kind === "supervisor_review" ? "评审人" : "登记人员"}
        <input name="actor" required maxLength={160} />
      </label>
      <label>
        实际发生时间
        <input
          name="occurred_at"
          aria-label="实际发生时间"
          type="datetime-local"
          step="1"
          required
          defaultValue={localNow()}
        />
      </label>
      {kind === "test_execution" ? (
        <>
          <label>
            实际测试环境
            <input name="environment" required maxLength={4000} />
          </label>
          <p className="hint">
            只登记实际执行的用例。未登记、未执行或缺少证据的必需用例不会通过。
          </p>
          {results.map((item, index) => (
            <fieldset className="material-case" key={item.version_id + item.id}>
              <legend>
                {item.id} · {item.title}
              </legend>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={item.selected}
                  onChange={(event) =>
                    setResults((before) =>
                      before.map((value, i) =>
                        i === index
                          ? { ...value, selected: event.target.checked }
                          : value,
                      ),
                    )
                  }
                />
                登记 {item.id}
              </label>
              {item.selected ? (
                <>
                  <label>
                    结果 · {item.id}
                    <select
                      aria-label={`结果 · ${item.id}`}
                      value={item.status}
                      onChange={(event) =>
                        setResults((before) =>
                          before.map((value, i) =>
                            i === index
                              ? { ...value, status: event.target.value }
                              : value,
                          ),
                        )
                      }
                    >
                      {["NOT_RUN", "PASS", "FAIL", "BLOCKED"].map((value) => (
                        <option key={value} value={value}>
                          {materialLabels[value]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    实际结果 · {item.id}
                    <textarea
                      aria-label={`实际结果 · ${item.id}`}
                      required
                      maxLength={4000}
                      value={item.actual}
                      onChange={(event) =>
                        setResults((before) =>
                          before.map((value, i) =>
                            i === index
                              ? { ...value, actual: event.target.value }
                              : value,
                          ),
                        )
                      }
                    />
                  </label>
                  <label>
                    用例证据 · {item.id}
                    <input
                      required
                      maxLength={4000}
                      value={item.evidence}
                      onChange={(event) =>
                        setResults((before) =>
                          before.map((value, i) =>
                            i === index
                              ? { ...value, evidence: event.target.value }
                              : value,
                          ),
                        )
                      }
                    />
                  </label>
                </>
              ) : null}
            </fieldset>
          ))}
        </>
      ) : null}
      <label>
        意见与说明
        <textarea name="reason" required maxLength={8000} rows={3} />
      </label>
      <label>
        证据引用
        <textarea
          name="evidence"
          required
          maxLength={4000}
          placeholder="评审记录链接、测试报告路径或工单编号"
        />
      </label>
      {kind === "acceptance" ? (
        <p className="hint">
          确认当前材料及手工登记测试结果。全项目交付仍需后续 M5 验收。
        </p>
      ) : null}
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="primary" disabled={busy}>
        {busy ? "正在登记…" : "保存记录"}
      </button>
    </form>
  );
}

export function DocumentEditor({
  version,
  save,
}: {
  version: MaterialVersion;
  save: (data: Record<string, unknown>) => Promise<void>;
}) {
  const [content, setContent] = useState<MaterialContent>(() =>
    structuredClone(version.content),
  );
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function patchCase(index: number, value: Record<string, unknown>) {
    setContent((before) => ({
      ...before,
      cases: before.cases?.map((item, i) =>
        i === index ? { ...item, ...value } : item,
      ),
    }));
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await save({
        kind: version.kind,
        expected_version: version.id,
        content,
        reason,
      });
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="form-stack">
      <p className="hint">
        基于 v{version.number}{" "}
        保存新版本，原内容保留。引用旧版本的评审与测试结果需要重新确认。
      </p>
      <label>
        材料标题
        <input
          required
          maxLength={200}
          value={content.title}
          onChange={(event) =>
            setContent((before) => ({ ...before, title: event.target.value }))
          }
        />
      </label>
      {version.kind === "summary_design" ? (
        <label>
          设计正文（Markdown）
          <textarea
            aria-label="设计正文（Markdown）"
            required
            rows={16}
            maxLength={60000}
            value={content.markdown}
            onChange={(event) =>
              setContent((before) => ({
                ...before,
                markdown: event.target.value,
              }))
            }
          />
        </label>
      ) : (
        <>
          <label>
            测试环境
            <textarea
              aria-label="测试环境"
              required
              maxLength={4000}
              value={content.environment}
              onChange={(event) =>
                setContent((before) => ({
                  ...before,
                  environment: event.target.value,
                }))
              }
            />
          </label>
          {version.kind === "performance_plan" ? (
            <>
              <label>
                负载模型
                <textarea
                  aria-label="负载模型"
                  required
                  maxLength={4000}
                  value={content.workload}
                  onChange={(event) =>
                    setContent((before) => ({
                      ...before,
                      workload: event.target.value,
                    }))
                  }
                />
              </label>
              {content.metrics?.map((metric, index) => (
                <div className="form-stack material-case" key={index}>
                  <label>
                    指标名称 · {index + 1}
                    <input
                      required
                      value={metric.name}
                      maxLength={200}
                      onChange={(event) =>
                        setContent((before) => ({
                          ...before,
                          metrics: before.metrics?.map((value, i) =>
                            i === index
                              ? { ...value, name: event.target.value }
                              : value,
                          ),
                        }))
                      }
                    />
                  </label>
                  <label>
                    目标 · {index + 1}
                    <input
                      required
                      maxLength={2000}
                      value={metric.target}
                      onChange={(event) =>
                        setContent((before) => ({
                          ...before,
                          metrics: before.metrics?.map((value, i) =>
                            i === index
                              ? { ...value, target: event.target.value }
                              : value,
                          ),
                        }))
                      }
                    />
                  </label>
                </div>
              ))}
            </>
          ) : null}
          {content.cases?.map((item, index) => (
            <fieldset className="material-case form-stack" key={index}>
              <legend>用例 {index + 1}</legend>
              <label>
                用例编号 · {index + 1}
                <input
                  required
                  pattern="[A-Za-z0-9_-]{1,64}"
                  value={item.id}
                  onChange={(event) =>
                    patchCase(index, { id: event.target.value })
                  }
                />
              </label>
              <label>
                用例名称 · {index + 1}
                <input
                  required
                  maxLength={200}
                  value={item.title}
                  onChange={(event) =>
                    patchCase(index, { title: event.target.value })
                  }
                />
              </label>
              <label>
                前置条件 · {index + 1}
                <textarea
                  aria-label={`前置条件 · ${index + 1}`}
                  maxLength={4000}
                  value={item.preconditions}
                  onChange={(event) =>
                    patchCase(index, { preconditions: event.target.value })
                  }
                />
              </label>
              <label>
                操作步骤 · {index + 1}
                <textarea
                  aria-label={`操作步骤 · ${index + 1}`}
                  required
                  value={item.steps.join("\n")}
                  onChange={(event) =>
                    patchCase(index, { steps: event.target.value.split("\n") })
                  }
                />
              </label>
              <label>
                预期结果 · {index + 1}
                <textarea
                  aria-label={`预期结果 · ${index + 1}`}
                  required
                  maxLength={4000}
                  value={item.expected}
                  onChange={(event) =>
                    patchCase(index, { expected: event.target.value })
                  }
                />
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={item.required}
                  onChange={(event) =>
                    patchCase(index, { required: event.target.checked })
                  }
                />
                必需用例 · {index + 1}
              </label>
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  setContent((before) => ({
                    ...before,
                    cases: before.cases?.filter((_, i) => i !== index),
                  }))
                }
              >
                移除此用例
              </button>
            </fieldset>
          ))}
          <button
            type="button"
            className="secondary"
            onClick={() =>
              setContent((before) => ({
                ...before,
                cases: [
                  ...(before.cases ?? []),
                  {
                    id: "C-" + crypto.randomUUID().slice(0, 8),
                    title: "",
                    preconditions: "",
                    steps: [""],
                    expected: "",
                    required: true,
                  },
                ],
              }))
            }
          >
            添加用例
          </button>
        </>
      )}
      <label>
        修订原因
        <input
          required
          maxLength={4000}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      {error ? (
        <p className="inline-error" role="alert">
          {error}
        </p>
      ) : null}
      <button className="primary" disabled={busy}>
        保存材料新版本
      </button>
    </form>
  );
}
