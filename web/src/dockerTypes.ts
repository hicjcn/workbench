export type DockerRecipe = { id: string; family: string; number: number; current: boolean; name: string; reference: string; dockerfile: string; actor: string; reason: string; created_at: number };
export type DockerImage = { id: string; tags: string[]; size: number; created_at: string };
export type DockerOperation = { id: string; kind: string; status: string; input: { reference: string; actor: string; reason: string }; error: string | null; result: unknown; created_at: number };
export type ManagedContainer = { id: string; name: string; workspace: string | null; kind: string; running: boolean; verified: boolean; orphan: boolean; image_id: string; cpu_limit: number; memory_limit: number; networks: string[] };
export type DockerState = { connected: boolean; stale: boolean; updated_at: number | null; trial: boolean; images_available: boolean; images: DockerImage[]; containers: ManagedContainer[]; recipes: DockerRecipe[]; operations: DockerOperation[] };
export const imageActions: Record<string, string> = { pull: "拉取镜像", build: "构建镜像", remove: "移除镜像标签" };
export const operationStatus: Record<string, string> = { QUEUED: "排队中", RUNNING: "执行中", UNKNOWN: "待核实", SUCCEEDED: "已完成", FAILED: "失败", CANCELED: "已取消", ACKNOWLEDGED: "已人工结束核实" };
