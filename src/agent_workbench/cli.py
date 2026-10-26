from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import runtime
from .workspace import WorkbenchError, create_workspace, load_workspace, snapshot


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent-workbench")
    parser.add_argument("--config", help="统一 TOML 配置文件")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    create = commands.add_parser("create")
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--root", required=True, type=Path)
    create.add_argument("--requirement", required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--ref", default="HEAD")
    create.add_argument("--include-dirty", action="store_true")
    create.add_argument("--include-untracked", action="store_true")
    for name in ("snapshot", "start", "probe", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True, type=Path)
        if name == "start":
            command.add_argument("--image")
            command.add_argument("--proxy-ca", type=Path)
    args = parser.parse_args()
    from .configuration import configured
    configuration = configured(args)
    try:
        if args.command == "doctor":
            result = runtime.doctor()
        elif args.command == "create":
            result = create_workspace(args.source, args.root, args.requirement, args.project,
                                      args.ref, args.include_dirty, args.include_untracked)
        elif args.command == "snapshot":
            path, _ = load_workspace(args.workspace)
            result = snapshot(path / "repo")
        elif args.command == "start":
            result = runtime.start(args.workspace, args.image or configuration.tool_image, args.proxy_ca or configuration.proxy_ca)
        elif args.command == "probe":
            result = runtime.probe(args.workspace)
        else:
            result = runtime.stop(args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "doctor" and not result["docker_available"] else 0
    except (WorkbenchError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1

