export type MaterialCase = {
  id: string;
  title: string;
  preconditions: string;
  steps: string[];
  expected: string;
  required: boolean;
};
export type MaterialContent = {
  title: string;
  markdown?: string;
  environment?: string;
  cases?: MaterialCase[];
  workload?: string;
  metrics?: { name: string; target: string }[];
};
export type MaterialVersion = {
  id: string;
  kind: string;
  number: number;
  content: MaterialContent;
  content_digest: string;
  provenance: string;
  reason: string;
  validity: string;
  created_at: number;
};
export type MaterialRecord = {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  validity: string;
  created_at: number;
};
export type MaterialRound = {
  id: string;
  flow_id: string;
  revision_id: string;
  basis_digest: string;
  context_digest: string;
  context_valid: boolean;
  stage: string;
  next_step: string | null;
  documents: Record<string, MaterialVersion | null>;
  version_history: Omit<MaterialVersion, "content">[];
  supervisor: MaterialRecord | null;
  performance: MaterialRecord | null;
  supervisor_valid: boolean;
  performance_valid: boolean;
  performance_decision: string;
  test_status: string;
  accepted: boolean;
  cases: (MaterialCase & {
    version_id: string;
    plan_kind: string;
    status: string;
    result: { actual: string; evidence: string } | null;
  })[];
  outstanding_feedback: MaterialRecord[];
  feedback_history: MaterialRecord[];
  records: MaterialRecord[];
  exports: {
    id: string;
    sha256: string;
    size: number;
    created_at: number;
    manifest: { status_at_export: string };
  }[];
  attempts: {
    job_id: string;
    step_key: string;
    status: string;
    error: string | null;
    created_at: number;
  }[];
};
export type MaterialState = {
  can_start: boolean;
  current: MaterialRound | null;
  history: { id: string; state: string; reason: string; created_at: number }[];
};
export const materialNames: Record<string, string> = {
  summary_design: "概要设计",
  test_plan: "功能测试单",
  performance_plan: "性能测试单",
  supervisor_review: "主管评审",
  performance_decision: "性能测试判断",
  test_execution: "测试执行登记",
  acceptance: "材料阶段验收",
  feedback: "反馈",
  resolve_feedback: "反馈处置",
  edit_document: "人工修订",
  export: "材料包导出",
};
export const materialLabels: Record<string, string> = {
  SUMMARY_DESIGN: "等待概要设计",
  SUPERVISOR_REVIEW: "等待主管评审",
  PERFORMANCE_DECISION: "性能判断待明确",
  TEST_PLAN: "等待功能测试单",
  PERFORMANCE_PLAN: "等待性能测试单",
  TEST_EXECUTION: "等待测试执行记录",
  FEEDBACK_RESOLUTION: "等待反馈处置",
  ACCEPTANCE: "等待材料阶段验收",
  ACCEPTED: "材料阶段已验收",
  STALE: "需重新确认",
  CURRENT: "当前有效",
  HISTORICAL: "历史记录",
  UNDECIDED: "待明确",
  REQUIRED: "需要性能测试",
  NOT_REQUIRED: "不需要性能测试",
  PASS: "登记通过",
  FAIL: "登记失败",
  BLOCKED: "执行受阻",
  NOT_RUN: "尚未执行",
  APPROVED: "通过",
  REJECTED: "退回",
  BUG: "Bug",
  REQUIREMENT_CHANGE: "需求变更",
  ARCHITECTURE_CHANGE: "架构变更",
  DOCUMENT_COMMENT: "文档意见",
  RESOLVED: "已解决",
  ACTIVE: "进行中",
};
