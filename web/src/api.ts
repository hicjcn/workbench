export type Snapshot = {
  tree_digest: string;
  commit_sha: string;
  files: { path: string; sha256: string }[];
};
export type Workspace = {
  path: string;
  environment: string;
  approval: string;
  snapshot: Snapshot | null;
  diff: string | null;
  diff_truncated: boolean;
};
export type Project = {
  id: string;
  name: string;
  source_path: string;
  image: string;
};
export type Requirement = {
  id: string;
  title: string;
  description: string;
  acceptance: string;
  project_id: string;
  created_at: number;
  workspace: Omit<Workspace, "snapshot" | "diff">;
};
export type Job = {
  id: string;
  action: string;
  step_key?: string | null;
  status: string;
  error: string | null;
  cancel_requested: boolean;
  created_at: number;
  result?: { diff_truncated?: boolean } | null;
};
export type Review = {
  id: string;
  digest: string;
  decision: string;
  reason: string;
  created_at: number;
};
export type Detail = Omit<Requirement, "workspace"> & {
  workspace: Workspace;
  project: Project;
  jobs: Job[];
  reviews: Review[];
  profile: import("./ProjectProfilePanel").RuntimeProfile | null;
  preflight: { status: string; image_id: string; checks: { name: string; passed: boolean }[] } | null;
};
export type ExecutionEvent = {
  id: number;
  kind: string;
  payload: { text?: string };
  created_at: number;
};
export const active = (job: Job) =>
  job.status === "QUEUED" || job.status === "RUNNING";
export const labels: Record<string, string> = {
  NOT_CREATED: "未创建",
  STOPPED: "已停止",
  RUNNING: "运行中",
  ERROR: "需要处理",
  UNKNOWN: "待核实",
  QUEUED: "排队中",
  SUCCEEDED: "已完成",
  FAILED: "执行失败",
  CANCELED: "已取消",
  PENDING: "待自审",
  STALE: "需重新确认",
  APPROVED: "自审通过",
  REJECTED: "已退回",
  prepare: "创建工作区",
  start: "启动环境",
  stop: "停止环境",
  refresh: "刷新代码差异",
  task: "Agent 任务",
  reconcile: "停止并核实",
  review: "人工代码确认",
  workflow: "流程步骤",
  materials: "材料步骤",
  delivery: "跨项目交付",
  preflight: "环境预检",
  build: "项目构建",
  verify: "项目验证",
};
export const timeLabel = (timestamp: number) =>
  new Date(timestamp * 1000).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });

export const jobLabel = (job: Job) => {
  const names: Record<string, string> = {
    summary_design: "概要设计",
    test_plan: "功能测试单",
    performance_plan: "性能测试单",
    supervisor_review: "主管评审",
    performance_decision: "性能判断",
    test_execution: "测试执行登记",
    acceptance: "材料验收",
    feedback: "反馈",
    resolve_feedback: "反馈处置",
    edit_document: "修订材料",
    export: "材料导出",
    analysis: "只读分析",
    scope: "范围确认",
    development: "开发",
    verification: "执行验证",
    resolution: "问题处理",
    self_review: "流程自审",
  };
  if (job.step_key?.startsWith("model_review_"))
    return `模型评审 ${Number(job.step_key.slice(-1)) + 1}`;
  return (
    (job.step_key && names[job.step_key]) || labels[job.action] || job.action
  );
};

export async function api<T>(
  path: string,
  body?: unknown,
  method = body === undefined ? "GET" : "POST",
): Promise<T> {
  let response: Response;
  try {
    response = await fetch("/api/v1" + path, {
      method,
      credentials: "same-origin",
      headers:
        body === undefined
          ? {}
          : {
              "Content-Type": "application/json",
              "Idempotency-Key": crypto.randomUUID(),
            },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("无法连接工作台。请查看执行记录，确认操作是否已经接收。");
  }
  let value;
  try {
    value = await response.json();
  } catch {
    throw new Error("控制服务返回异常，请查看服务日志。");
  }
  if (!response.ok) {
    const detail = value.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item: { msg: string }) => item.msg).join("；")
          : "操作失败，请查看执行记录",
    );
  }
  return value as T;
}
