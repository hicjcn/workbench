"""Export source snapshots into independent, credential-free Git repositories."""

from __future__ import annotations

import hashlib
import io
import json
import os
import ntpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from .platform_support import IS_WINDOWS, is_link


class WorkbenchError(Exception):
    pass


def git(directory: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-C", str(directory), *args],
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise WorkbenchError(f"Git operation failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def excluded(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if IS_WINDOWS:
        parts = tuple(part.casefold() for part in parts)
    return any(
        p in {".git", "node_modules", "__pycache__", ".venv", "target", "runtime-secrets", "credentials"}
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
        temporary.replace(path)
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
        if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            raise WorkbenchError("Git LFS pointers are not materialized by M1")
        digest = hashlib.sha256(content).hexdigest()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise WorkbenchError("Source changed while computing its snapshot")
        entries.append({"path": name, "sha256": digest, "executable": executable.get(name, False) if IS_WINDOWS else bool(after.st_mode & 0o111)})
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"tree_digest": hashlib.sha256(encoded).hexdigest(), "files": entries}


def snapshot(repo: Path) -> dict:
    repo = repo.resolve(strict=True)
    first = inventory(repo)
    second = inventory(repo)
    if first != second:
        raise WorkbenchError("Workspace changed while capturing its snapshot; stop the writer first")
    return {"commit_sha": git(repo, "rev-parse", "HEAD").decode().strip(), **first}


def create_workspace(source: Path, root: Path, requirement: str, project: str,
                     ref: str = "HEAD", include_dirty: bool = False,
                     include_untracked: bool = False) -> dict:
    for value in (requirement, project):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value):
            raise WorkbenchError("Requirement and project IDs must be lowercase identifiers of at most 48 characters")
    source = source.resolve(strict=True)
    root = root.resolve()
    if root.is_relative_to(source):
        raise WorkbenchError("Workspace root must be outside the source repository")
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
    declared_size = 0
    for line in git(source, "ls-tree", "-r", "-l", source_sha).decode().splitlines():
        size = line.split("\t", 1)[0].split()[-1]
        if size.isdigit():
            declared_size += int(size)
    if declared_size > 100 * 1024 * 1024:
        raise WorkbenchError("M1 source export exceeds the 100 MiB limit")
    source_before = inventory(source, untracked=include_untracked) if include_dirty else None
    destination = safe_path(root, f"{requirement}/{project}")
    if destination.exists():
        raise WorkbenchError("Workspace already exists; it will not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{project}-", dir=destination.parent))
    try:
        repo = temporary / "repo"
        repo.mkdir()
        archive = git(source, "archive", "--format=tar", source_sha)
        total = 0
        executable_paths = []
        case_paths = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar:
                if excluded(member.name) or member.isdir():
                    continue
                if IS_WINDOWS:
                    windows_names([member.name], case_paths)
                path = safe_path(repo, member.name)
                if not member.isfile():
                    raise WorkbenchError(f"Unsupported archived entry: {member.name}")
                total += member.size
                if total > 100 * 1024 * 1024:
                    raise WorkbenchError("M1 source export exceeds the 100 MiB limit")
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                if stream is None:
                    raise WorkbenchError("Source archive entry cannot be read")
                content = stream.read()
                if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                    raise WorkbenchError("Git LFS pointers are not materialized by M1")
                with path.open("xb") as output:
                    output.write(content)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
                if member.mode & 0o111:
                    executable_paths.append(member.name)
        git(repo, "init", "-b", f"req/{requirement}")
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "config", "core.eol", "lf")
        git(repo, "config", "user.name", "Agent Workbench Snapshot")
        git(repo, "config", "user.email", "snapshot@agent-workbench.invalid")
        git(repo, "add", "--all")
        if IS_WINDOWS:
            for name in executable_paths:
                git(repo, "update-index", "--chmod=+x", "--", name)
        git(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", f"Source snapshot {source_sha}")
        initial = snapshot(repo)
        if include_dirty:
            actual = {entry["path"] for entry in source_before["files"]}
            for entry in initial["files"]:
                if entry["path"] not in actual:
                    safe_path(repo, entry["path"]).unlink()
            for name in actual:
                path = safe_path(repo, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(safe_path(source, name), path)
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
        write_json(temporary / "workspace.json", manifest)
        temporary.rename(destination)
        return {"workspace": str(destination), "source_commit": source_sha,
                "snapshot": snapshot(destination / "repo")}
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

