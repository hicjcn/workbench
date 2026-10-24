"""Run a real free-model coding task against a fresh, non-sensitive fixture."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_workbench import runtime
from agent_workbench.providers import openrouter_catalog_model as shared_catalog_model
from agent_workbench.workspace import WorkbenchError, create_workspace, git, snapshot, write_json
from verify_isolation import progress, wait_ready


PROMPT = """This is an approved, small infrastructure validation task in a disposable sample repository.
The agreed plan and acceptance criteria are:
1. In server.mjs, greeting(name) must trim leading/trailing whitespace from a supplied string.
2. If the name is missing or consists only of whitespace, return {message: 'hello world'}.
3. Preserve named greetings such as 'hello developer'.
4. Add regression tests in server.test.mjs for padded names and whitespace-only names.
5. Run node --test server.test.mjs and fix any failures.
The specification and plan are approved; implement and verify them without clarification questions.
Use the installed Superpowers skills as appropriate. Do not dispatch subagents, push Git, access
the network through tools, or create containers. Keep changes within this sample repository.
Finish with a concise summary and the actual test results.
"""


def redact(value, secret_values=()):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value or ""
    for secret in secret_values:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def save_agent_output(root, stdout, stderr, secret_values=()):
    """Preserve even TimeoutExpired's partial byte output without exposing it in the report."""
    events = []
    for filename, value in (("agent-events.jsonl", stdout), ("agent-stderr.txt", stderr)):
        value = redact(value, secret_values)
        path = root / filename
        path.touch(mode=0o600)
        path.write_text(value)
        if filename == "agent-events.jsonl":
            for line in value.splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except json.JSONDecodeError:
                    pass
    return events


def selected_provider(model):
    if model.startswith("openrouter/"):
        runtime.openrouter_config(model)
        return "openrouter"
    if model.startswith("opencode/"):
        runtime.opencode_free_config(model)
        return "opencode"
    raise WorkbenchError("Select an OpenCode free model or an explicit openrouter/vendor/model:free ID")


def openrouter_catalog_model(model):
    """Retain the verification script's injectable transport for regression tests."""
    return shared_catalog_model(model, fetch=urlopen)


def task_passed(report):
    return (report["agent_exit_code"] == 0 and report["agent_error_events"] == 0
            and report["model_response_observed"] and report["implementation_file_changed"]
            and report["regression_test_file_changed"] and report["test_count"] >= 4
            and report["source_unchanged"] and report["tests_passed"]
            and report["acceptance_behavior_passed"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--image", default="agent-workbench-m1:local")
    parser.add_argument("--model", default="openrouter/nvidia/nemotron-3-ultra-550b-a55b:free")
    parser.add_argument("--proxy-ca", type=Path)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    provider = selected_provider(args.model)
    secret_values = ()
    if provider == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise WorkbenchError("Configure the existing OPENROUTER_API_KEY secret before OpenRouter verification")
        secret_values = (os.environ["OPENROUTER_API_KEY"],)
    root = args.root.resolve()
    if root.exists():
        raise WorkbenchError("Verification root must be new")
    root.mkdir(parents=True)
    source = root / "source"
    shutil.copytree(Path(__file__).parent / "sample-project", source)
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "M1 Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "add", "--all")
    git(source, "-c", "commit.gpgsign=false", "commit", "-m", "Initialize free-model fixture")
    value = create_workspace(source, root / "workspaces", "req-free", "backend")
    workspace = Path(value["workspace"])
    before = snapshot(workspace / "repo")
    write_json(root / "before-snapshot.json", before)
    (root / "approved-task.txt").write_text(PROMPT)
    started = time.monotonic()
    report = {"status": "RUNNING", "model": args.model, "provider": provider, "credentials_injected": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "timeout_seconds": args.timeout,
              "source_tree_digest": snapshot(source)["tree_digest"], "model_response_observed": False}
    exit_code = 1
    try:
        if provider == "openrouter":
            report["catalog_model"] = openrouter_catalog_model(args.model)
            report["free_pricing_verified"] = True
        progress("start-free-model-runtime")
        if provider == "openrouter":
            state = runtime.start(workspace, args.image, args.proxy_ca, openrouter_model=args.model)
            report["credentials_injected"] = True
            report["credential_source"] = "OPENROUTER_API_KEY"
        else:
            state = runtime.start(workspace, args.image, args.proxy_ca, opencode_model=args.model)
        report["capabilities"] = wait_ready(workspace)
        available = runtime.docker("exec", state["container"], "opencode", "models", provider).stdout.splitlines()
        report["model_listed"] = args.model in available
        if args.model not in available:
            raise WorkbenchError("Selected free model is not listed by the installed OpenCode version")
        progress("submit-free-model-task")
        # Submit once. A timeout is UNKNOWN, never an instruction to replay the task.
        report["submission_attempted"] = True
        result = runtime.docker(
            "exec", state["container"], "opencode", "run",
            "--attach", "http://127.0.0.1:4096", "--model", args.model,
            "--format", "json", PROMPT, check=False, timeout=args.timeout,
        )
        events = save_agent_output(root, result.stdout, result.stderr, secret_values)
        report["agent_exit_code"] = result.returncode
        report["event_types"] = sorted({event.get("type", "unknown") for event in events})
        report["model_response_observed"] = any(event.get("type") in {"text", "tool_use", "step_finish"} for event in events)
        report["session_ids"] = sorted({event["sessionID"] for event in events if isinstance(event.get("sessionID"), str)})
        failures = [event for event in events if event.get("type") == "error"]
        report["agent_error_events"] = len(failures)
        after = snapshot(workspace / "repo")
        write_json(root / "after-snapshot.json", after)
        report["code_changed"] = before["tree_digest"] != after["tree_digest"]
        before_files = {entry["path"]: entry["sha256"] for entry in before["files"]}
        after_files = {entry["path"]: entry["sha256"] for entry in after["files"]}
        report["implementation_file_changed"] = before_files.get("server.mjs") != after_files.get("server.mjs")
        report["regression_test_file_changed"] = before_files.get("server.test.mjs") != after_files.get("server.test.mjs")
        report["before_digest"] = before["tree_digest"]
        report["after_digest"] = after["tree_digest"]
        report["source_unchanged"] = snapshot(source)["tree_digest"] == report["source_tree_digest"]
        progress("verify-generated-code")
        tests = runtime.docker("exec", state["container"], "node", "--test", "--test-reporter=tap", "server.test.mjs", check=False)
        (root / "test-output.txt").write_text(redact(tests.stdout + tests.stderr, secret_values))
        behavior = runtime.docker(
            "exec", state["container"], "node", "--input-type=module", "-e",
            "import assert from 'node:assert/strict'; import {greeting} from './server.mjs'; "
            "assert.equal(greeting('  developer  ').message, 'hello developer'); "
            "assert.equal(greeting('   ').message, 'hello world'); "
            "assert.equal(greeting().message, 'hello world');", check=False,
        )
        (root / "acceptance-output.txt").write_text(redact(behavior.stdout + behavior.stderr, secret_values))
        report["tests_passed"] = tests.returncode == 0
        count = re.search(r"^# tests (\d+)\s*$", tests.stdout, re.MULTILINE)
        report["test_count"] = int(count.group(1)) if count else 0
        report["acceptance_behavior_passed"] = behavior.returncode == 0
        if task_passed(report):
            report["status"] = "PASSED_REAL_MODEL_TASK"
            exit_code = 0
        else:
            report["status"] = "FAILED_OR_BLOCKED_MODEL_TASK"
        baseline = json.loads((workspace / "workspace.json").read_text())["source_baseline"]["commit_sha"]
        (root / "code-diff.patch").write_text(redact(git(workspace / "repo", "diff", "--binary", baseline), secret_values))
    except subprocess.TimeoutExpired as error:
        save_agent_output(root, error.stdout, error.stderr, secret_values)
        report["status"] = "UNKNOWN_TASK_STATE_AFTER_TIMEOUT"
    except WorkbenchError as error:
        report["status"] = "FAILED_OR_BLOCKED"
        report["diagnostic"] = redact(str(error), secret_values)
    finally:
        progress("stop-free-model-runtime")
        if (workspace / "runtime.json").exists():
            try:
                runtime.stop(workspace)
                report["runtime_stopped"] = True
            except (WorkbenchError, subprocess.TimeoutExpired):
                report["runtime_stopped"] = False
                report["status"] = "NEEDS_CLEANUP"
                exit_code = 1
        if report["status"] == "RUNNING":
            report["status"] = "FAILED"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

