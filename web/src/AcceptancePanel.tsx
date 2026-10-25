import { useState } from "react";
import { api } from "./api";

type Audit = {
  status: string;
  checks: { key: string; title: string; status: string; category: string }[];
  note: string;
};

export function AcceptancePanel({ requirementId }: { requirementId: string }) {
  const [audit, setAudit] = useState<Audit | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function refresh() {
    setBusy(true);
    setError("");
    try {
      setAudit(await api<Audit>(`/requirements/${requirementId}/acceptance`));
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel acceptance-panel">
      <div className="section-title">
        <h2>完整流程验收证据</h2>
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void refresh()}
        >
          {busy ? "正在核对…" : "核对验收清单"}
        </button>
      </div>
      <p className="hint">
        检查当前流程的模型结束事件、技能使用、验证、评审和反馈记录。生成材料与实际测试分别核对，清单不会提交批准或自动重试任务。
      </p>
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      {audit && (
        <>
          <strong>
            {audit.status === "INCOMPLETE"
              ? "仍有待补齐的执行证据"
              : "证据记录齐全，待人工核查"}
          </strong>
          <ul>
            {audit.checks.map((item) => (
              <li key={item.key}>
                {item.status === "RECORDED" ? "✓ 已记录" : "○ 待完成"} ·{" "}
                {item.title}
                {item.category === "human_registration" ? "（人工登记）" : ""}
              </li>
            ))}
          </ul>
          <p className="hint">{audit.note}</p>
        </>
      )}
    </section>
  );
}
