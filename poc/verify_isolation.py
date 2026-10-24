"""Exercise two real Docker/OpenCode environments without invoking a model."""

import argparse
import json
from pathlib import Path
import shutil
import time

from agent_workbench import runtime
from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot, write_json


def progress(stage):
    print(json.dumps({"stage": stage}), flush=True)


def wait_ready(workspace):
    deadline = time.monotonic() + 90
    last_error = None
    while time.monotonic() < deadline:
        try:
            return runtime.probe(workspace)
        except WorkbenchError as error:
            last_error = error
            time.sleep(1)
    raise WorkbenchError(f"Runtime did not become ready: {last_error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", default="agent-workbench-m1:local")
    parser.add_argument("--proxy-ca", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists():
        raise WorkbenchError("Verification root must be new; existing evidence will not be overwritten")
    root.mkdir(parents=True)
    source = root / "source"
    shutil.copytree(Path(__file__).parent / "sample-project", source)
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "M1 Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "add", "--all")
    git(source, "-c", "commit.gpgsign=false", "commit", "-m", "Initialize isolation fixture")
    original = snapshot(source)
    workspaces = []
    report = {"status": "RUNNING", "checks": {}, "models_invoked": False}
    try:
        for requirement in ("req-a", "req-b"):
            value = create_workspace(source, root / "workspaces", requirement, "backend")
            workspaces.append(Path(value["workspace"]))
        first, second = workspaces
        progress("start-two-runtimes")
        first_state = runtime.start(first, args.image, args.proxy_ca)
        second_state = runtime.start(second, args.image, args.proxy_ca)
        progress("probe-first-runtime")
        first_report = wait_ready(first)
        progress("probe-second-runtime")
        second_report = wait_ready(second)
        for capabilities in (first_report, second_report):
            assert capabilities["health"]["healthy"]
            assert capabilities["superpowers_configured"]
            assert {"brainstorming", "writing-plans", "using-superpowers"} <= set(capabilities.get("skill_names", []))
            assert capabilities["empty_session_create_abort"] == "PASSED"
            assert not capabilities["runtime"]["privileged"]
            assert not capabilities["runtime"]["docker_socket_mounted"]
        report["checks"]["opencode_control_and_superpowers_skill_loading"] = "PASSED"
        assert first_state["base_url"] != second_state["base_url"]
        assert first_state["network"] != second_state["network"]
        report["checks"]["separate_networks_and_control_ports"] = "PASSED"

        runtime.docker("exec", first_state["container"], "node", "-e",
                       "require('node:fs').appendFileSync('server.mjs', '\\n// requirement-a-only\\n')")
        runtime.docker("exec", first_state["container"], "node", "-e",
                       "require('node:fs').writeFileSync('/home/node/persistence-marker', 'keep')")
        assert "requirement-a-only" in (first / "repo/server.mjs").read_text()
        assert "requirement-a-only" not in (second / "repo/server.mjs").read_text()
        assert snapshot(source) == original
        assert snapshot(first / "repo")["tree_digest"] != snapshot(second / "repo")["tree_digest"]
        report["checks"]["container_write_isolated_from_other_requirement_and_source"] = "PASSED"
        for state in (first_state, second_state):
            runtime.docker("exec", state["container"], "node", "--test", "server.test.mjs")
        report["checks"]["sample_tests_in_both_containers"] = "PASSED"

        preserved = snapshot(first / "repo")
        progress("rebuild-first-runtime")
        runtime.stop(first)
        assert runtime.probe(second)["health"]["healthy"]
        recreated = runtime.start(first, args.image, args.proxy_ca)
        rebuilt_report = wait_ready(first)
        assert snapshot(first / "repo") == preserved
        marker = runtime.docker("exec", recreated["container"], "node", "-e",
                                "process.stdout.write(require('node:fs').readFileSync('/home/node/persistence-marker'))")
        assert marker.stdout == "keep"
        assert runtime.probe(second)["health"]["healthy"]
        report["checks"]["rebuild_preserves_code_and_agent_home"] = "PASSED"
        report["checks"]["other_requirement_survives_rebuild"] = "PASSED"
        report["capabilities"] = rebuilt_report
        report["model_validation"] = {"status": "NOT_RUN_BY_INFRASTRUCTURE_CHECK",
                                      "reason": "Run verify_free_model.py separately for real model validation"}
        report["status"] = "PASSED_INFRASTRUCTURE_ONLY"
    finally:
        progress("stop-verification-runtimes")
        cleanup_errors = []
        for workspace in workspaces:
            if (workspace / "runtime.json").exists():
                try:
                    runtime.stop(workspace)
                except WorkbenchError as error:
                    cleanup_errors.append(str(error))
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            report["status"] = "NEEDS_CLEANUP"
        elif report["status"] == "RUNNING":
            report["status"] = "FAILED"
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

