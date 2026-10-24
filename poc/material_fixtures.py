"""Explicitly controlled reports for acceptance scripts; never imported by production."""
from datetime import datetime, timezone
import json

from agent_workbench import runtime
from agent_workbench.execution import Executor
from agent_workbench.workspace import snapshot, write_json
from agent_workbench.workflow import Workflow


def evidence(**extra):
    return {"actor": "受控验收登记人", "occurred_at": datetime.now(timezone.utc).isoformat(),
        "reason": "受控验收记录，非实际主管结论或业务测试证据", "evidence": "fixture://controlled-evidence", **extra}


def sample_document(kind):
    if kind == "summary_design":
        return {"title": "受控概要设计 · 未调用模型", "markdown": "## 范围\n仅用于材料链路验收。\n## 接口与数据\n保持样例接口。\n## 兼容与回滚\n保留原基线。\n## 风险与验证\n实际业务方案待确认。"}
    value = {"title": "受控性能测试单" if kind == "performance_plan" else "受控功能测试单", "environment": "隔离样例环境", "cases": [
        {"id": "P1" if kind == "performance_plan" else "F1", "title": "受控必需用例", "preconditions": "样例就绪", "steps": ["执行受控检查"], "expected": "符合受控验收目标", "required": True}]}
    if kind == "performance_plan":
        value.update(workload="10 并发持续 1 分钟", metrics=[{"name": "P95", "target": "小于 200ms（目标，非实测）"}])
    return value


def complete_code_fixture(database, settings, requirement_id, workspace):
    workflow = Workflow(database, settings.model)
    observed = Executor(settings).observe(workspace)
    workflow.start(requirement_id, ["opencode/glm-5.3-flash-free", "opencode/big-pickle"], "node --test", "M4 prerequisite: explicitly controlled M3 reports; no inference", observed["snapshot"]["tree_digest"])
    for _ in range(12):
        flow = workflow.read(requirement_id)["current"]
        step = flow["next_step"]
        if not step:
            assert flow["state"] == "COMPLETE"
            return flow
        request = {"prompt": "", "decision": "APPROVED", "reason": "受控前置确认，非实际业务审批", "dispositions": {}}
        workflow.enqueue(requirement_id, flow["id"], step, request, None)
        job = database.claim("controlled_m3_prerequisite")
        report = {"summary": "受控分析", "impacted_paths": [], "plan": "受控范围", "risks": []} if step == "analysis" else {"summary": "受控开发", "verification_notes": "仅准备 M4 验收前置"} if step == "development" else {"command": ["node", "--test"], "exit_code": 0} if step == "verification" else {"summary": "受控评审", "findings": []} if step.startswith("model_review_") else request
        database.finish(job["id"], "controlled_m3_prerequisite", job["epoch"], "SUCCEEDED", {**observed, "workflow_output": report}, None)
    raise AssertionError("Controlled prerequisite did not complete")


class ControlledMaterialExecutor(Executor):
    def task(self, job, workspace, emit, canceled, **options):
        state = json.loads((workspace / "runtime.json").read_text())
        details = json.loads(runtime.docker("inspect", state["container"]).stdout)[0]
        mount = next(item for item in details["Mounts"] if item["Destination"] == "/workspace/project")
        assert not mount["RW"] and options["read_only"]
        attempted = runtime.docker("exec", state["container"], "node", "-e", "require('node:fs').writeFileSync('/workspace/project/forbidden.txt','must fail')", check=False)
        assert attempted.returncode != 0
        path = self.settings.data_dir / "controlled-material-roles.json"
        records = json.loads(path.read_text()) if path.exists() else []
        records.append({"kind": job["input"]["step_key"], "code_digest": snapshot(workspace / "repo")["tree_digest"],
            "workspace": str(workspace), "source_read_only": True, "write_denied": True})
        write_json(path, records)
        emit("fixture", {"text": "Explicitly controlled M4 document report; no model inference"})
        return {**self.observe(workspace), "agent_text": json.dumps(sample_document(job["input"]["step_key"]), ensure_ascii=False)}
