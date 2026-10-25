"""Operator-driven sample acceptance; one durable intent per advance, human gates pause."""
import argparse
import fcntl
import json
import shlex
from pathlib import Path
import shutil
import sys
import uuid
from urllib.parse import urlparse

import httpx

from .model_diagnostics import diagnose
from .profiles import Content
from .runtime import model_config
from .workspace import WorkbenchError, git, write_json, snapshot
from .checked_archive import file_hash

CONTRACT_TARGET = "/run/workbench-config/acceptance.test.mjs"
VERIFY = "node --test --test-reporter=tap " + CONTRACT_TARGET
MODEL_STEPS = {"analysis", "development", "summary_design", "test_plan", "performance_plan"}


def plan(state, detail, workflow, material, group, negative_verified):
    """Pure next-action selection. Never construct a human decision or retry an old job."""
    if state.get("pending"): return {"status": "UNKNOWN_REQUEST", "intent": state["pending"]["key"]}
    active = [job for job in detail["jobs"] if job["status"] in ("QUEUED", "RUNNING", "UNKNOWN")]
    if active: return {"status": "BLOCKED_UNKNOWN" if any(j["status"] == "UNKNOWN" for j in active) else "WAITING_JOB", "jobs": [j["id"] for j in active]}
    failed = [job["id"] for job in detail["jobs"] if job["status"] in ("FAILED", "CANCELED") and job["id"] not in state.get("acknowledged", []) and not (negative_verified and job["id"] == state["resources"].get("negative_job_id"))]
    if failed: return {"status": "PAUSED_FAILURE", "jobs": failed}
    req = state["resources"]["requirement_id"]; base = f"/requirements/{req}"
    def action(kind, assign=None): return {"status": "READY", "path": base + "/actions", "body": {"action": kind}, "assign": assign, "model_call": False}
    workspace = detail["workspace"]
    if not workspace.get("snapshot"): return action("prepare")
    preflight = detail.get("preflight")
    if not preflight or preflight["status"] != "PASSED" or preflight.get("profile_digest") != detail["profile"]["content_digest"]:
        return action("stop") if workspace["environment"] == "RUNNING" else action("preflight")
    if workspace["environment"] != "RUNNING": return action("start")
    negative = state["resources"].get("negative_job_id")
    if not negative: return action("verify", "negative_job_id")
    if not negative_verified: return {"status": "BLOCKED_NEGATIVE_CONTROL", "job_id": negative}
    if not group["panorama"]["analysis"]:
        return {"status": "READY", "path": f"/deliveries/{group['id']}/panorama", "body": {"kind": "panorama", "context_digest": group["panorama"]["context_digest"], "model": state["models"][0], "prompt": "联合分析本样例的接口、验收条件与风险。", **evidence()}, "model_call": True}
    flow = workflow.get("current")
    if not flow:
        return {"status": "READY", "path": base + "/workflow", "body": {"review_models": state["models"][1:], "verification_command": VERIFY, "reason": "M10 完整样例在线验收", "digest": workspace["snapshot"]["tree_digest"]}, "model_call": False}
    if flow["state"] not in ("ACTIVE", "COMPLETE"): return {"status": "WAITING_HUMAN", "gate": "请在界面核实失效原因并明确开启新一轮流程"}
    if flow["state"] == "ACTIVE":
        step = flow["next_step"]
        if step in ("scope", "resolution", "self_review"): return {"status": "WAITING_HUMAN", "gate": step}
        if step == "development" and not group["scope_approved"]: return {"status": "WAITING_HUMAN", "gate": "cross_project_scope"}
        return {"status": "READY", "path": base + "/workflow/steps", "body": {"flow_id": flow["id"], "step_key": step, "prompt": "严格按已固定需求与只读验收测试实现；实际读取并使用 Superpowers 技能，保留工具日志。", "decision": None, "reason": "", "dispositions": {}}, "model_call": step != "verification"}
    current = material.get("current")
    if not current or not current["context_valid"]:
        return {"status": "READY", "path": base + "/materials", "body": {"flow_id": flow["id"], "digest": flow["basis_digest"], "reason": "M10 当前代码材料验收"}, "model_call": False}
    if not current["accepted"]:
        step = current["next_step"]
        if step not in ("summary_design", "test_plan", "performance_plan"): return {"status": "WAITING_HUMAN", "gate": step}
        return {"status": "READY", "path": base + "/materials/steps", "body": {"round_id": current["id"], "step_key": step, "context_digest": current["context_digest"], "prompt": "以实际样例代码和固定验收测试为依据，不编造审批或测试结果。", "data": {}}, "model_call": True}
    if not current["exports"]:
        return {"status": "READY", "path": base + "/materials/steps", "body": {"round_id": current["id"], "step_key": "export", "context_digest": current["context_digest"], "prompt": "", "data": {}}, "model_call": False}
    if not group["integration_valid"]: return {"status": "WAITING_HUMAN", "gate": "integration_registration"}
    if not any(item["validity"] == "CURRENT" for item in group["exports"]):
        return {"status": "READY", "path": f"/deliveries/{group['id']}/operations", "body": {"kind": "capture", "context_digest": group["context_digest"], **evidence()}, "model_call": False}
    return {"status": "WAITING_FINAL_REVIEW", "gate": "核对执行证据、反馈复评复测和交付包后，人工签收完整样例"}


def evidence():
    from datetime import datetime, timezone
    return {"actor": "样例执行工具", "reason": "执行已登记的 M10 样例操作，不代替人工批准", "evidence": "本工作台样例执行日志", "occurred_at": datetime.now(timezone.utc).isoformat()}


class Session:
    def __init__(self, root, api, token_file, transport=None):
        self.root = root.resolve(); self.file = self.root / "acceptance.json"
        parsed = urlparse(api)
        if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1") or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise WorkbenchError("验收工具只连接本机 HTTP 工作台根地址")
        if token_file.is_symlink(): raise WorkbenchError("令牌文件不能是符号链接")
        token = token_file.read_text().strip()
        if len(token) < 32: raise WorkbenchError("令牌文件不正确")
        self.client = httpx.Client(base_url=api.rstrip("/") + "/api/v1", headers={"Authorization": "Bearer " + token}, trust_env=False, transport=transport, timeout=30)
        if self.file.exists():
            if self.file.is_symlink(): raise WorkbenchError("验收日志不能是符号链接")
            self.state = json.loads(self.file.read_text())
            if self.state.get("schema") != "workbench-sample-acceptance/v1" or self.state["api"] != api.rstrip("/"): raise WorkbenchError("验收目录或 API 不匹配")
        else:
            if self.root.exists(): raise WorkbenchError("初始化需要尚不存在的独立目录")
            self.root.mkdir(parents=True, mode=0o700)
            self.state = {"schema": "workbench-sample-acceptance/v1", "api": api.rstrip("/"), "run_id": uuid.uuid4().hex, "resources": {}, "operations": [], "pending": None, "acknowledged": []}
            self.save()

    def save(self): write_json(self.file, self.state)

    def get(self, path):
        response = self.client.get(path); response.raise_for_status(); return response.json()

    def post(self, path, body, assign=None):
        if self.state["pending"]: raise WorkbenchError("上次请求结果不明，禁止自动重发；请先核实并 resolve")
        intent = {"key": f"m10-{self.state['run_id']}-{len(self.state['operations'])}", "path": path, "body": body, "assign": assign}
        self.state["pending"] = intent; self.save()
        try: response = self.client.post(path, json=body, headers={"Idempotency-Key": intent["key"]})
        except httpx.HTTPError: raise WorkbenchError("请求响应不明，意图已保留；不自动重发") from None
        if response.status_code >= 500: raise WorkbenchError("服务端请求结果未核实；意图已保留，不自动重发")
        if response.status_code >= 400:
            self.state["operations"].append({"key": intent["key"], "path": path, "status": response.status_code})
            self.state["pending"] = None; self.save()
            raise WorkbenchError(f"工作台拒绝请求（HTTP {response.status_code}），请查看界面状态")
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("id"), str): raise WorkbenchError("响应缺少资源标识，意图已保留，请核实")
        self.accept_response(value)
        return value

    def accept_response(self, value):
        intent = self.state["pending"]
        if intent["assign"]: self.state["resources"][intent["assign"]] = value["id"]
        self.state["operations"].append({"key": intent["key"], "path": intent["path"], "id": value.get("id"), "status": "RECEIVED"})
        self.state["pending"] = None; self.save()

    def resolve(self, identity):
        intent = self.state["pending"]
        if not intent: raise WorkbenchError("没有待核实请求")
        import re
        if not re.fullmatch(r"[a-z]+_[a-f0-9]{32}", identity): raise WorkbenchError("请提供工作台实际资源标识")
        path, body = intent["path"], intent["body"]
        if identity.startswith("job_"):
            value = self.get("/jobs/" + identity)
            valid = value.get("idempotency_key") == intent["key"] and value["requirement_id"] == self.state["resources"].get("requirement_id")
        elif path == "/projects":
            value = next((item for item in self.get("/projects") if item["id"] == identity), {})
            valid = all(value.get(key) == wanted for key, wanted in body.items())
        elif path == "/requirements":
            value = self.get("/requirements/" + identity); valid = all(value.get(key) == wanted for key, wanted in body.items())
        elif path.endswith("/profiles"):
            value = next((item for item in self.get(path)["history"] if item["id"] == identity), {})
            expected = json.loads(json.dumps(body["content"]))
            for mount in expected["mounts"]: mount["sha256"] = file_hash(Path(mount["source"]))
            valid = value.get("content") == expected and value.get("actor") == body["actor"] and value.get("reason") == body["reason"] and value.get("number") == 1
        elif path == "/deliveries":
            value = self.get("/deliveries/" + identity)
            valid = value.get("state") == "ACTIVE" and value["revision"]["content"] == body
        elif path.endswith("/workflow"):
            value = self.get(path + "/runs/" + identity)
            valid = value.get("is_current") and value.get("reason") == body["reason"] and value.get("basis_digest") == body["digest"] and value["config"]["development_model"] == self.state["models"][0] and value["config"]["review_models"] == body["review_models"] and value["config"]["verification_argv"] == shlex.split(body["verification_command"])
        elif path.endswith("/materials"):
            value = self.get(path + "?round_id=" + identity)["current"]
            valid = value and value["is_current"] and value["flow_id"] == body["flow_id"] and value["basis_digest"] == body["digest"] and value["reason"] == body["reason"]
        else: raise WorkbenchError("该请求类型不能自动核实，请保留意图检查实际记录")
        if not valid: raise WorkbenchError("资源与固定请求意图不一致")
        self.accept_response(value); return {"status": "RESOLVED", "resource_id": identity}

    def setup(self, image, reviewers):
        if self.state["pending"]: raise WorkbenchError("初始化请求未核实，不能自动重发")
        config = self.get("/config")
        selected = [config["model"], *reviewers]
        for model in selected: model_config(model)
        if not 2 <= len(set(reviewers)) == len(reviewers) <= 4: raise WorkbenchError("需要 2 至 4 个不同评审模型")
        if self.state.get("models") and self.state["models"] != selected: raise WorkbenchError("验收角色模型已固定，不能静默修改")
        self.state["models"] = selected
        if self.state.get("image", image) != image: raise WorkbenchError("样例镜像已固定，不能静默修改")
        self.state["image"] = image
        template = Path(__file__).resolve().parents[2] / "poc/acceptance-sample"
        source = self.root / "source"; contract = self.root / "criteria/acceptance.test.mjs"
        if not source.exists():
            source.mkdir(); contract.parent.mkdir()
            for filename in ("server.mjs", "package.json"): shutil.copyfile(template / filename, source / filename)
            shutil.copyfile(template / "acceptance.test.mjs", contract)
            git(source, "init", "-b", "main"); git(source, "config", "user.name", "M10 Sample"); git(source, "config", "user.email", "sample@example.invalid")
            git(source, "add", "--all"); git(source, "-c", "commit.gpgsign=false", "commit", "-m", "Initial acceptance sample")
            self.state["contract_sha256"] = file_hash(contract)
            self.state["source_tree_digest"] = snapshot(source)["tree_digest"]; self.save()
        self.check_contract()
        resources = self.state["resources"]
        if "project_id" not in resources: self.post("/projects", {"name": "M10 完整验收样例", "source_path": str(source), "image": image}, "project_id")
        if "profile_id" not in resources:
            content = Content(image=image, build_command="node --check server.mjs", verification_command=VERIFY,
                mounts=[{"source": str(contract), "target": CONTRACT_TARGET}], application={"start_command": "node server.mjs", "port": 8080, "health_path": "/"}).model_dump()
            self.post(f"/projects/{resources['project_id']}/profiles", {"expected_profile": None, "content": content, "actor": "样例执行工具", "reason": "固定独立验收条件，只读挂载"}, "profile_id")
        if "requirement_id" not in resources:
            self.post("/requirements", {"project_id": resources["project_id"], "title": "支持中英文问候并规范名字输入", "description": "保留现有英文默认值；greeting(name, lang) 对名字 trim，空白默认 world，lang=zh 返回你好，其他语言回退英文。HTTP 查询 name/lang 必须使用相同规则。使用 Superpowers 开发、两个不同模型评审并登记人工检查、概要设计和实际测试。",
                "acceptance": "固定只读 acceptance.test.mjs 全部通过；HTTP 返回 JSON。完成材料后至少实际演练一次反馈修复、复评和复测。"}, "requirement_id")
        if "group_id" not in resources:
            self.post("/deliveries", {"title": "M10 完整样例交付", "description": "单项目样例全链路，不代表真实业务验收。", "members": [{"requirement_id": resources["requirement_id"], "required": True, "skip_reason": ""}], "actor": "样例执行工具", "reason": "固定验收范围", "expected_revision": None}, "group_id")
        return {"status": "SETUP", "resources": resources, "contract_sha256": self.state["contract_sha256"]}

    def check_contract(self):
        if not self.state.get("contract_sha256") or not self.state.get("source_tree_digest"): raise WorkbenchError("样例初始化未完整，请保留目录核实，不自动覆盖源码")
        if file_hash(self.root / "criteria/acceptance.test.mjs") != self.state["contract_sha256"]: raise WorkbenchError("验收测试已变化，不能沿用原验收结论")
        if snapshot(self.root / "source")["tree_digest"] != self.state["source_tree_digest"]: raise WorkbenchError("原始样例源码已变化，不能沿用初始验收基线")

    def snapshot(self):
        self.check_contract(); req = self.state["resources"]["requirement_id"]
        detail = self.get("/requirements/" + req); workflow = self.get(f"/requirements/{req}/workflow")
        material = self.get(f"/requirements/{req}/materials"); group = self.get("/deliveries/" + self.state["resources"]["group_id"])
        profile = detail.get("profile")
        if profile and (profile["id"] != self.state["resources"]["profile_id"] or profile["content"]["verification_command"] != VERIFY or
            not any(item["target"] == CONTRACT_TARGET and item["sha256"] == self.state["contract_sha256"] for item in profile["content"]["mounts"])):
            raise WorkbenchError("固定运行配置或独立验收测试不匹配")
        current = workflow.get("current")
        if current and (current["config"]["verification_argv"] != shlex.split(VERIFY) or current["config"]["review_models"] != self.state["models"][1:] or current["config"]["development_model"] != self.state["models"][0]):
            raise WorkbenchError("当前流程的模型或验证命令已改变，不能沿用固定样例验收")
        negative = self.state["resources"].get("negative_job_id"); verified = False
        if negative:
            job = self.get("/jobs/" + negative); texts = []
            after = 0
            while True:
                batch = self.get("/jobs/" + negative + "/events?after=" + str(after))
                for event in batch:
                    if event["kind"] == "verification": texts.append(event["payload"].get("text", ""))
                if len(batch) < 200: break
                after = batch[-1]["id"]
                if len(texts) > 10000: raise WorkbenchError("负例验证日志超过核对限制")
            import re
            joined = "\n".join(texts)
            verified = job["status"] == "FAILED" and "验证命令退出码：1" in joined and bool(re.search(r"# fail [1-9]\d*", joined))
        return detail, workflow, material, group, verified

    def status(self):
        if self.state["pending"]: return {"status": "UNKNOWN_REQUEST", "intent": self.state["pending"]["key"]}
        detail, workflow, material, group, verified = self.snapshot()
        return {"next": plan(self.state, detail, workflow, material, group, verified), "negative_control_confirmed": verified,
            "audit": self.get(f"/requirements/{detail['id']}/acceptance"), "delivery_current": any(item["validity"] == "CURRENT" for item in group["exports"])}

    def advance(self, retry_job=None):
        if self.state["pending"]: return {"status": "UNKNOWN_REQUEST", "intent": self.state["pending"]["key"]}
        if self.get("/config")["model"] != self.state["models"][0]: raise WorkbenchError("API 开发模型与固定验收配置不一致")
        if retry_job:
            job = self.get("/jobs/" + retry_job)
            if job["requirement_id"] != self.state["resources"]["requirement_id"] or job["status"] not in ("FAILED", "CANCELED"): raise WorkbenchError("只能明确重试本样例已失败或已取消的任务")
            if retry_job not in self.state["acknowledged"]: self.state["acknowledged"].append(retry_job)
            if self.state["resources"].get("negative_job_id") == retry_job: self.state["resources"].pop("negative_job_id")
            self.save()
        action = plan(self.state, *self.snapshot())
        if action["status"] != "READY": return action
        if action["model_call"]:
            readiness = diagnose(self.state["models"]); write_json(self.root / "model-readiness.json", readiness)
            if not readiness["can_attempt"]: return {"status": readiness["status"], "diagnostics": readiness}
        value = self.post(action["path"], action["body"], action.get("assign"))
        return {"status": "SUBMITTED", "id": value["id"], "model_call": action["model_call"]}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["doctor", "setup", "status", "advance", "resolve"])
    parser.add_argument("--root", type=Path); parser.add_argument("--api", default="http://127.0.0.1:8340"); parser.add_argument("--token-file", type=Path)
    parser.add_argument("--image", default="agent-workbench-m1:local"); parser.add_argument("--review-model", action="append")
    parser.add_argument("--retry-job"); parser.add_argument("--job-id", "--resource-id", dest="resource_id"); args = parser.parse_args()
    reviewers = args.review_model or ["opencode/glm-5.3-flash-free", "opencode/big-pickle"]
    session = None
    try:
        if args.command == "doctor": result = diagnose(reviewers)
        else:
            if args.root is None or args.token_file is None: raise WorkbenchError("请指定独立 --root 和现有 --token-file，不在参数中填写令牌内容")
            if args.command != "setup" and not (args.root / "acceptance.json").is_file(): raise WorkbenchError("请先 setup 初始化独立验收目录")
            # A sibling lock also covers exclusive creation of a brand new root.
            lock_path = args.root.resolve().with_name(args.root.name + ".acceptance.lock"); lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a") as lock:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise WorkbenchError("另一个验收命令正在运行") from None
                session = Session(args.root, args.api, args.token_file)
                if args.command == "setup": result = session.setup(args.image, reviewers)
                elif args.command == "status": result = session.status()
                elif args.command == "resolve": result = session.resolve(args.resource_id or "")
                else: result = session.advance(args.retry_job)
                write_json(session.root / "last-result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        result_status = result.get("status") or result.get("next", {}).get("status")
        return 2 if result_status in ("BLOCKED_NETWORK", "MISSING_CREDENTIAL", "UNKNOWN_REQUEST", "BLOCKED_UNKNOWN", "PAUSED_FAILURE", "BLOCKED_NEGATIVE_CONTROL") else 0
    except (WorkbenchError, OSError, ValueError, httpx.HTTPError) as error:
        # HTTP exceptions may carry request headers; do not stringify them.
        message = str(error) if isinstance(error, WorkbenchError) else "验收命令失败，请检查本机服务、文件和私有运行记录"
        print(json.dumps({"status": "BLOCKED", "error": message}, ensure_ascii=False), file=sys.stderr); return 1
    finally:
        if session: session.client.close()


if __name__ == "__main__": raise SystemExit(main())
