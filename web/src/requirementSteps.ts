import type { Detail } from "./api";
import type { MaterialState } from "./materialTypes";
import type { WorkflowState } from "./WorkflowPanel";

export function workflowPhase(
  key: string,
): "analysis" | "development" | "review" {
  if (key === "analysis" || key === "scope") return "analysis";
  if (key === "development" || key === "verification") return "development";
  return "review";
}

export const requirementSteps = [
  {
    title: "准备环境",
    description:
      "先创建本需求独立的代码副本，再预检并启动 Agent 环境。原项目源码会保留。",
  },
  {
    title: "分析与方案",
    description:
      "开启开发流程，让 Agent 阅读候选仓库并分析影响。检查分析结果后，由你确认修改范围与方案。",
  },
  {
    title: "开发与验证",
    description:
      "按已确认的方案执行 Superpowers 开发，再运行验证命令。验证通过后进入代码评审。",
  },
  {
    title: "评审与自审",
    description:
      "依次运行独立模型评审，处理发现的问题，再检查代码差异并登记你的自审结论。",
  },
  {
    title: "设计与测试",
    description:
      "生成概要设计并登记主管评审，判断是否需要性能测试，再准备测试单、执行测试并处理反馈。",
  },
  {
    title: "验收交付",
    description:
      "核对分析、开发、评审、设计和测试的完整证据，再按团队流程完成交付。",
  },
];

export function currentRequirementStep(
  detail: Detail,
  workflow: WorkflowState | null,
  materials: MaterialState | null,
): number {
  if (!detail.workspace.snapshot) return 0;
  const flow = workflow?.current;
  if (!flow) {
    if (detail.workspace.environment === "DEBUG_RUNNING") return 0;
    if (detail.profile && detail.preflight?.status !== "PASSED") return 0;
    if (
      detail.workspace.environment !== "RUNNING" &&
      !detail.jobs.some(
        (job) => job.action === "start" && job.status === "SUCCEEDED",
      )
    )
      return 0;
    return 1;
  }
  if (flow.state !== "ACTIVE" && flow.state !== "COMPLETE") return 1;
  if (flow.state === "COMPLETE") {
    const round = materials?.current;
    return round?.flow_id === flow.id && round.context_valid && round.accepted
      ? 5
      : 4;
  }
  const phase = workflowPhase(flow.next_step ?? "analysis");
  return phase === "analysis" ? 1 : phase === "development" ? 2 : 3;
}

export function preparationAction(detail: Detail): {
  action: string;
  label: string;
  description: string;
} {
  if (!detail.workspace.snapshot)
    return {
      action: "prepare",
      label: "创建工作区，开始准备",
      description: "从已登记项目复制代码，为本需求创建独立工作区。",
    };
  if (detail.workspace.environment === "DEBUG_RUNNING")
    return {
      action: "stop",
      label: "停止排障环境，继续准备",
      description: "排障检查完成后停止容器，再执行预检并启动 Agent 环境。代码与记录会保留。",
    };
  if (detail.profile && detail.preflight?.status !== "PASSED") {
    if (detail.workspace.environment === "RUNNING")
      return {
        action: "stop",
        label: "先停止环境，准备预检",
        description: "环境预检需要在开发环境停止时运行。代码与记录会保留。",
      };
    return {
      action: "preflight",
      label: "运行环境预检",
      description:
        "检查镜像、Agent CLI、开发工具和配置。失败时可查看执行记录定位缺项。",
    };
  }
  return {
    action: "start",
    label: "启动环境，进入分析",
    description: "启动本需求的容器和 Agent CLI，成功后进入分析与方案。",
  };
}
