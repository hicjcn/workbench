"""Export source snapshots into independent, credential-free Git repositories."""

from __future__ import annotations

import hashlib
import json
import os
import ntpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from .platform_support import IS_WINDOWS, is_link, retry_file_access


class WorkbenchError(Exception):
    pass


def git(directory: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
         *(["-c", "core.longpaths=true"] if IS_WINDOWS else []), "-C", str(directory), *args],
        capture_output=True,
        input=input_bytes,
        timeout=120,
    )
    if result.returncode:
        raise WorkbenchError(f"Git operation failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def index_executable(repo, names, *, enabled=True):
    """One Git process even for thousands of Windows executable entries."""
    if names:
        git(repo, "update-index", "--chmod=" + ("+x" if enabled else "-x"), "-z", "--stdin",
            input_bytes=b"".join(name.encode("utf-8") + b"\0" for name in sorted(names)))


def excluded(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if IS_WINDOWS:
        parts = tuple(part.casefold() for part in parts)
    return any(
        p in {".git", "node_modules", "__pycache__", ".venv", "target", "runtime-secrets", "credentials", "workbench.toml", ".workbench-run", ".local-data"}
        or p == ".env" or p.startswith(".env.")
        for p in parts
    )


def windows_names(names, seen=None):
    """Do not silently merge distinct Linux paths on a case-insensitive volume."""
    seen = {} if seen is None else seen
    for name in names:
        parts = PurePosixPath(name).parts
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            if seen.setdefault(prefix.casefold(), prefix) != prefix:
                raise WorkbenchError("Source contains case-colliding paths that Windows cannot represent")


def safe_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or "\\" in name or ntpath.splitdrive(name)[0]:
        raise WorkbenchError("Source contains an unsafe path")
    if IS_WINDOWS and any(part.endswith((" ", ".")) or any(char in part for char in '<>:"|?*') or
        re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part) for part in relative.parts):
        raise WorkbenchError("Source contains a name that Windows cannot safely represent")
    result = root.joinpath(*relative.parts)
    # The prototype rejects all symlinks instead of following an external target.
    current = result
    while current != root:
        if is_link(current):
            raise WorkbenchError(f"Symlinks are not supported by M1: {name}")
        current = current.parent
    return result


def write_json(path: Path, value: dict, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A private, unpredictable temporary file cannot follow a persisted Agent-created link.
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        # Windows readers normally omit FILE_SHARE_DELETE. A short concurrent
        # read must not abort a control-state transition before stop is sent.
        retry_file_access(lambda: temporary.replace(path))
    finally:
        temporary.unlink(missing_ok=True)


def inventory(repo: Path, *, untracked: bool = True) -> dict:
    args = ["ls-files", "--cached", "-z"]
    if untracked:
        args += ["--others", "--exclude-standard"]
    names = set(git(repo, *args).decode().rstrip("\0").split("\0")) - {""}
    if IS_WINDOWS:
        windows_names(name for name in names if not excluded(name))
    executable = {}
    if IS_WINDOWS:
        # NTFS chmod does not express POSIX execute bits. Keep the Git index
        # contract instead, including scripts exported from Linux repositories.
        for item in git(repo, "ls-files", "--stage", "-z").decode().split("\0"):
            if item:
                metadata, name = item.split("\t", 1)
                executable[name] = metadata.split()[0] == "100755"
    entries = []
    for name in sorted(names):
        if excluded(name):
            continue
        path = safe_path(repo, name)
        if not path.exists():
            continue  # A deletion is represented by absence from the tree.
        if not path.is_file():
            raise WorkbenchError(f"Unsupported source entry: {name}")
        before = path.stat()
        content = path.read_bytes()
        if content.startswith((b"version https://git-lfs.github.com/spec/v1\n", b"version https://git-lfs.github.com/spec/v1\r\n")):
            raise WorkbenchError("Git LFS pointers are not materialized by M1")
        digest = hashlib.sha256(content).hexdigest()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise WorkbenchError("Source changed while computing its snapshot")
        entries.append({"path": name, "sha256": digest, "executable": executable.get(name, False) if IS_WINDOWS else bool(after.st_mode & 0o111)})
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"tree_digest": hashlib.sha256(encoded).hexdigest(), "files": entries}


def snapshot(repo: Path, *, single_repository=False) -> dict:
    repo = repo.resolve(strict=True)
    from .multi_repo import layout, snapshot as multi_snapshot
    configured = None if single_repository else layout(repo)
    if configured: return multi_snapshot(repo, configured)
    first = inventory(repo)
    second = inventory(repo)
    if first != second:
        raise WorkbenchError("Workspace changed while capturing its snapshot; stop the writer first")
    return {"commit_sha": git(repo, "rev-parse", "HEAD").decode().strip(), **first}


def create_workspace(source: Path, root: Path, requirement: str, project: str,
                     ref: str = "HEAD", include_dirty: bool = False,
                     include_untracked: bool = False, repository_paths=None, shared_files=None, _single_repository=False) -> dict:
    for value in (requirement, project):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value):
            raise WorkbenchError("Requirement and project IDs must be lowercase identifiers of at most 48 characters")
    if repository_paths:
        from .multi_repo import source_root
        source = source_root(source)
    source = source.resolve(strict=True)
    root = root.resolve()
    if root.is_relative_to(source):
        raise WorkbenchError("Workspace root must be outside the source repository")
    from .multi_repo import layout, create as create_multi
    if not _single_repository and (repository_paths or layout(source)):
        if ref != "HEAD": raise WorkbenchError("多仓库工作区目前固定各仓库的 HEAD")
        if include_untracked and not include_dirty: raise WorkbenchError("Including untracked files requires --include-dirty")
        return create_multi(source, root, requirement, project, repositories=repository_paths, shared_files=shared_files or [],
            include_dirty=include_dirty, include_untracked=include_untracked)
    if Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve() != source:
        raise WorkbenchError("Source must be a Git repository root")
    if include_untracked and not include_dirty:
        raise WorkbenchError("Including untracked files requires --include-dirty")
    if include_dirty and ref != "HEAD":
        raise WorkbenchError("Dirty overlays are supported only with --ref HEAD")
    source_sha = git(source, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    tree = git(source, "ls-tree", "-r", source_sha).decode()
    if any(line.startswith("160000 ") for line in tree.splitlines()):
        raise WorkbenchError("Git submodules are not supported by M1")
    if include_dirty and any(line.startswith("120000 ") for line in tree.splitlines()):
        raise WorkbenchError("含已提交符号链接的原项目请先提交改动，再使用 HEAD 创建工作区；暂不支持脏文件叠加")
    declared_size = 0
    for line in git(source, "ls-tree", "-r", "-l", source_sha).decode().splitlines():
        size = line.split("\t", 1)[0].split()[-1]
        if size.isdigit():
            declared_size += int(size)
    if declared_size > 100 * 1024 * 1024:
        raise WorkbenchError("M1 source export exceeds the 100 MiB limit")
    from . import logging_support as logs
    with logs.operation("workspace", "source.input_snapshot", "读取副本来源的文件清单"):
        source_before = inventory(source, untracked=include_untracked) if include_dirty else None
    destination = safe_path(root, f"{requirement}/{project}")
    if destination.exists():
        raise WorkbenchError("Workspace already exists; it will not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{project}-", dir=destination.parent))
    try:
        repo = temporary / "repo"
        repo.mkdir()
        # Git archive applies host checkout conversion too. In particular the
        # Git for Windows global autocrlf default must not rewrite source bytes.
        with logs.operation("workspace", "source.archive", "导出固定 Git 提交"):
            archive = git(source, "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=tar", source_sha)
        from .source_export import export
        with logs.operation("workspace", "source.copy", "复制已提交源码文件"):
            executable_paths, materialized_links, copied_link_files = export(archive, repo,
                read_link_blob=lambda name: git(source, "cat-file", "blob", f"{source_sha}:{name}"))
        with logs.operation("workspace", "source.git_init", "建立独立 Git 快照"):
            git(repo, "init", "-b", f"req/{requirement}")
            git(repo, "config", "core.autocrlf", "false")
            git(repo, "config", "core.eol", "lf")
            git(repo, "config", "user.name", "Agent Workbench Snapshot")
            git(repo, "config", "user.email", "snapshot@agent-workbench.invalid")
            git(repo, "add", "--all")
            if IS_WINDOWS:
                index_executable(repo, executable_paths)
            git(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", f"Source snapshot {source_sha}")
        with logs.operation("workspace", "source.baseline", "校验已提交源码基线"):
            initial = snapshot(repo)
        if include_dirty:
            with logs.operation("workspace", "source.overlay", "同步变更、新增及删除文件"):
                baseline = {entry["path"]: entry for entry in initial["files"]}
                actual = {entry["path"]: entry for entry in source_before["files"]}
                for name in baseline.keys() - actual.keys(): safe_path(repo, name).unlink()
                changed = [name for name, entry in actual.items() if baseline.get(name) != entry]
                for name in changed:
                    path = safe_path(repo, name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(safe_path(source, name), path)
                if IS_WINDOWS:
                    for runnable in (True, False):
                        index_executable(repo, [name for name in changed if name in baseline and
                            actual[name]['executable'] == runnable and baseline[name]['executable'] != runnable], enabled=runnable)
                logs.event("workspace", "source.overlay_count", "变更覆盖已完成", detailed=True, count=len(changed), total=len(actual))
                if source_before != inventory(source, untracked=include_untracked):
                    raise WorkbenchError("Source changed while copying; retry from a stable source")
                if source_sha != git(source, "rev-parse", "HEAD").decode().strip():
                    raise WorkbenchError("Source HEAD changed while copying")
        manifest = {
            "schema_version": 1, "requirement": requirement, "project": project,
            "source_commit": source_sha, "source_history": "snapshot_export",
            "repo": str(destination / "repo"), "source_baseline": initial,
            "include_dirty": include_dirty, "include_untracked": include_untracked,
        }
        if materialized_links:
            manifest["materialized_links"] = materialized_links
        if copied_link_files:
            manifest["copied_link_files"] = copied_link_files
        write_json(temporary / "workspace.json", manifest)
        with logs.operation("workspace", "source.final_snapshot", "校验独立源码副本"):
            verified = snapshot(repo)
        temporary.rename(destination)
        logs.event("workspace", "source.ready", "源码导出和独立 Git 快照已完成", detailed=True, count=len(initial['files']))
        return {"workspace": str(destination), "source_commit": source_sha,
                "snapshot": verified}
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_workspace(path: Path) -> tuple[Path, dict]:
    path = path.resolve(strict=True)
    manifest = json.loads((path / "workspace.json").read_text(encoding="utf-8"))
    if Path(manifest["repo"]).resolve() != path / "repo":
        raise WorkbenchError("Workspace manifest points outside its own repository")
    if is_link(path / "repo"):
        raise WorkbenchError("Workspace repository must not be a symlink")
    return path, manifest


def binary_diff(repo, baseline, files, *, prefix=""):
    """Complete, path-prefixed binary patch including untracked ordinary files."""
    names = sorted({item['path'] for item in files} | {item['path'] for item in baseline['files']})
    chunks = []
    options = [f'--src-prefix=a/{prefix}', f'--dst-prefix=b/{prefix}']
    for index in range(0, len(names), 100):
        chunks.append(git(repo, '--literal-pathspecs', 'diff', '--no-ext-diff', '--no-textconv', '--binary', *options,
            baseline['commit_sha'], '--', *names[index:index + 100]))
    tracked = set(git(repo, 'ls-files', '-z').decode().rstrip('\0').split('\0'))
    for name in names:
        if name in tracked or not safe_path(repo, name).is_file(): continue
        result = subprocess.run(['git', '-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false',
            *(['-c', 'core.longpaths=true'] if IS_WINDOWS else []), '-C', str(repo),
            'diff', '--no-index', '--no-ext-diff', '--no-textconv', '--binary', *options, '--', os.devnull, name], capture_output=True, timeout=120)
        if result.returncode not in (0, 1): raise WorkbenchError('新增文件差异采集失败')
        chunks.append(result.stdout)
    return b''.join(chunks)

