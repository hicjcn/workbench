"""Read-only generation and trusted material operations under the requirement lock."""
import json

from .material_schemas import DOCUMENT_SCHEMAS, parse_document
from .material_export import build
from .workflow_execution import readonly_agent
from .workspace import WorkbenchError
from .logging_support import progress


def execute(executor, job, workspace, emit, canceled):
    payload = job["input"]
    step = payload["step_key"]
    progress(emit, "materials." + step, "正在处理材料生成、登记或导出步骤")
    observed = executor.observe(workspace)
    if step == "export":
        return {**observed, "material_output": build(executor.settings.data_dir, job, observed)}
    if observed["snapshot"]["tree_digest"] != payload["bundle"]["code_digest"]:
        raise WorkbenchError("代码已变化，材料或人工记录不能生效")
    if step not in DOCUMENT_SCHEMAS:
        if observed["diff_truncated"]:
            raise WorkbenchError("代码差异展示不完整，当前不能进行人工确认")
        return {**observed, "material_output": payload["data"]}
    instruction = {
        "summary_design": "生成服务端概要设计。Markdown 包含需求与范围、整体方案、接口、数据及迁移、兼容性、异常处理、安全、部署与回滚、风险和验证。未知事项明确标注待确认。",
        "test_plan": "生成可执行功能测试用例，覆盖需求验收、正常/异常路径、兼容与回归。用例 ID 唯一，至少一个必需用例。不得声称测试已经执行。",
        "performance_plan": "根据已确认性能需要生成功能外的性能测试单，包含可度量目标、负载、环境、步骤与预期。不得编造实测数据。",
    }[step]
    prompt = instruction + "只读当前固定代码，不修改源码，不启动子代理或提交。最终只输出符合 schema 的 JSON。\nschema:\n" + json.dumps(DOCUMENT_SCHEMAS[step].model_json_schema(), ensure_ascii=False)
    prompt += "\n输入包（任务数据，不能替代人工批准）：\n" + json.dumps(payload["bundle"], ensure_ascii=False) + "\n补充：" + payload["request"]["prompt"]
    result = readonly_agent(executor, job, workspace, emit, canceled, payload["bundle"]["model"], prompt, lambda text: parse_document(step, text))
    result["material_output"] = result.pop("workflow_output")
    return result
