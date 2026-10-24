"""Fixed export paths outside Agent mounts; packages describe status at export."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile
from datetime import datetime, timezone

from .workspace import WorkbenchError


LIMIT = 16 * 1024 * 1024


def export_path(root, job_id):
    import re
    if not re.fullmatch(r"job_[a-f0-9]{32}", job_id):
        raise WorkbenchError("导出任务标识不正确")
    directory = root / "exports"
    if directory.is_symlink():
        raise WorkbenchError("导出目录不能是符号链接")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (job_id + ".zip")
    if path.is_symlink():
        raise WorkbenchError("导出文件不能是符号链接")
    return path


def render(kind, content):
    if kind == "summary_design":
        return f"# {content['title']}\n\n{content['markdown']}\n"
    lines = [f"# {content['title']}", "", "环境：" + content["environment"], ""]
    if kind == "performance_plan":
        lines += ["负载：" + content["workload"], *[f"- {metric['name']}：{metric['target']}" for metric in content["metrics"]], ""]
    for case in content["cases"]:
        lines += [f"## {case['id']} · {case['title']}", "", "必需：" + ("是" if case["required"] else "否"),
            "前置：" + case["preconditions"], *[f"{index}. {step}" for index, step in enumerate(case["steps"], 1)], "预期：" + case["expected"], ""]
    return "\n".join(lines)


def build(root, job, observed):
    state = job["input"]["export_state"]
    manifest = {"schema": "workbench-materials/v1", "round_id": state["id"], "flow_id": state["flow_id"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "revision_id": state["revision_id"], "code_digest": state["basis_digest"], "observed_code_digest": observed["snapshot"]["tree_digest"],
        "input_digest": job["input"]["input_digest"], "status_at_export": state["stage"] if state["basis_digest"] == observed["snapshot"]["tree_digest"] else "STALE",
        "test_status_at_export": state["test_status"], "performance_decision": state["performance_decision"],
        "performance_skip_reason": state["performance"]["payload"]["reason"] if state["performance_decision"] == "NOT_REQUIRED" else None,
        "scope": "materials only; code, external evidence files and full MVP delivery are not included", "entries": []}
    entries = {"records.json": json.dumps(state["records"], ensure_ascii=False, indent=2).encode(),
        "requirement.json": json.dumps(job["input"]["bundle"]["requirement_revision"], ensure_ascii=False, indent=2).encode(),
        "code-review.json": json.dumps(job["input"]["bundle"]["upstream"], ensure_ascii=False, indent=2).encode(),
        "feedback.json": json.dumps(state["feedback_history"], ensure_ascii=False, indent=2).encode(),
        "test-assessment.json": json.dumps({"cases": state["cases"], "test_status": state["test_status"], "outstanding_feedback": state["outstanding_feedback"]}, ensure_ascii=False, indent=2).encode(),
        "README.txt": "材料包记录导出时的版本和状态。材料生成不等于主管通过或测试通过；证据仅保存引用，完整项目交付另行验收。\n".encode()}
    for kind, version in state["documents"].items():
        if version:
            prefix = f"{kind}/v{version['number']}"
            entries[prefix + ".json"] = json.dumps(version, ensure_ascii=False, indent=2).encode()
            entries[prefix + ".md"] = render(kind, version["content"]).encode()
    if sum(len(content) for content in entries.values()) > LIMIT:
        raise WorkbenchError("材料包超过 16 MiB 限制")
    for name, content in entries.items():
        manifest["entries"].append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    content = buffer.getvalue()
    path = export_path(root, job["id"])
    if path.exists():
        raise WorkbenchError("该任务的导出已存在，不覆盖旧文件")
    fd, temporary = tempfile.mkstemp(prefix=".export-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        # Hard-link publication refuses to replace an existing path.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "manifest": manifest}


def verify_export(root, job_id, expected, input_digest=None):
    try:
        return _verify_export(root, job_id, expected, input_digest)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        raise WorkbenchError("导出文件或清单已损坏或丢失，不能下载") from None


def _verify_export(root, job_id, expected, input_digest=None):
    path = export_path(root, job_id)
    if path.stat().st_size > LIMIT or path.stat().st_size != expected["size"]:
        raise WorkbenchError("导出文件大小校验失败")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected["sha256"]:
        raise WorkbenchError("导出文件摘要校验失败")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest != expected["manifest"] or (input_digest and manifest["input_digest"] != input_digest):
            raise WorkbenchError("导出清单与执行输入不一致")
        if set(archive.namelist()) != {"manifest.json", *(item["path"] for item in manifest["entries"])}:
            raise WorkbenchError("导出条目与清单不一致")
        for item in manifest["entries"]:
            info = archive.getinfo(item["path"])
            if info.file_size != item["size"] or info.file_size > LIMIT:
                raise WorkbenchError("导出条目大小校验失败")
            if hashlib.sha256(archive.read(info)).hexdigest() != item["sha256"]:
                raise WorkbenchError("导出条目摘要校验失败")
    return path
