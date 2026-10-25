import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone

from .checked_archive import build as build_archive, verify as verify_archive
from .workspace import WorkbenchError, git, safe_path, snapshot


def package_path(root, job_id):
    import re
    if not re.fullmatch(r"job_[a-f0-9]{32}", job_id): raise WorkbenchError("交付任务标识不正确")
    directory = root / "deliveries"
    if directory.is_symlink(): raise WorkbenchError("交付目录不可信")
    return directory / (job_id + ".zip")


def full_diff(executor, workspace, files):
    baseline = json.loads((workspace / "workspace.json").read_text())["source_baseline"]
    names = sorted({item["path"] for item in files} | {item["path"] for item in baseline["files"]})
    chunks = []
    for index in range(0, len(names), 100):
        chunks.append(git(workspace / "repo", "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--binary", baseline["commit_sha"], "--", *names[index:index + 100]))
    tracked = set(git(workspace / "repo", "ls-files", "-z").decode().rstrip("\0").split("\0"))
    for name in names:
        if name in tracked or not safe_path(workspace / "repo", name).is_file(): continue
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(workspace / "repo"),
            "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--binary", "--", "/dev/null", name], capture_output=True, timeout=120)
        if result.returncode not in (0, 1): raise WorkbenchError("新增文件差异采集失败")
        chunks.append(result.stdout)
    content = b"".join(chunks)
    # Reject known control/model secrets rather than alter a patch or silently export them.
    if any(value.encode() in content for value in executor.secret_values(workspace)): raise WorkbenchError("差异包含已知凭据，不能生成普通交付包")
    return content


def build(executor, job, observations):
    payload = job["input"]
    header = {"schema": "workbench-delivery/v1", "group_id": payload["group_id"], "revision_id": payload["revision"]["id"],
        "context_digest": payload["context_digest"], "status_at_capture": "ARCHIVED" if payload["request"]["kind"] == "archive" else "READY",
        "captured_at": datetime.now(timezone.utc).isoformat(), "scope": payload["revision"]["content"],
        "scope_decision": payload["scope_decision"], "approval": payload["request"], "projects": []}
    header["integration"] = payload["integration"]
    entries, baselines = [], {}
    for member in payload["members"]:
        if not member["required"]:
            header["projects"].append({"project_id": member["project_id"], "requirement_id": member["requirement_id"], "status": "SKIPPED", "reason": member["skip_reason"]}); continue
        workspace = Path(member["workspace_path"]); repo = workspace / "repo"
        current = snapshot(repo); baselines[str(workspace)] = current["tree_digest"]
        if current["tree_digest"] != member["baseline"]["code_digest"] or current["commit_sha"] != member["baseline"]["commit_sha"]: raise WorkbenchError("实际代码与已验收基线不一致，请刷新差异")
        prefix = "projects/" + member["project_id"]
        manifest = json.loads((workspace / "workspace.json").read_text())
        header["projects"].append({"project_id": member["project_id"], "requirement_id": member["requirement_id"], "status": "READY",
            "baseline": member["baseline"], "source_commit": manifest["source_commit"], "source_baseline": manifest["source_baseline"],
            "runtime_profile": manifest.get("runtime_profile")})
        entries.extend((prefix + "/code/" + item["path"], safe_path(repo, item["path"])) for item in current["files"])
        for item in current["files"]:
            data = safe_path(repo, item["path"]).read_bytes()
            if any(value.encode() in data for value in executor.secret_values(workspace)): raise WorkbenchError("源码包含已知凭据，交付已阻止")
        entries.append((prefix + "/changes.patch", full_diff(executor, workspace, current["files"])))
        if (workspace / "capability-report.json").exists(): entries.append((prefix + "/environment.json", workspace / "capability-report.json"))
        if (workspace / "preflight-report.json").exists(): entries.append((prefix + "/preflight.json", workspace / "preflight-report.json"))
        entries.append((prefix + "/history.json", json.dumps(member["history"], ensure_ascii=False, indent=2).encode()))
    entries.append(("README.txt", "交付包固定各项目代码和结论，不会推送分支、合并代码或部署。外部证据保留引用；后续代码/需求变化使当前交付需要重新核实。\n".encode()))
    result = build_archive(package_path(executor.settings.data_dir, job["id"]), header, entries)
    for workspace, expected in baselines.items():
        if snapshot(Path(workspace) / "repo")["tree_digest"] != expected: raise WorkbenchError("采集过程中代码变化，交付包不能生效")
    return result


def verify(root, job_id, expected, context_digest=None):
    manifest = verify_archive(package_path(root, job_id), expected)
    if manifest["schema"] != "workbench-delivery/v1" or (context_digest and manifest["context_digest"] != context_digest): raise WorkbenchError("交付包与固定输入不一致")
    return package_path(root, job_id)
