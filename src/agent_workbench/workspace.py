"""Export source snapshots into independent, credential-free Git repositories."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


class WorkbenchError(Exception):
    pass


def git(directory: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise WorkbenchError(f"Git operation failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def excluded(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return any(
        p in {".git", "node_modules", "__pycache__", ".venv", "target", "runtime-secrets", "credentials"}
        or p == ".env" or p.startswith(".env.")
        for p in parts
    )


def safe_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise WorkbenchError("Source contains an unsafe path")
    result = root.joinpath(*relative.parts)
    # The prototype rejects all symlinks instead of following an external target.
    current = result
    while current != root:
        if current.is_symlink():
            raise WorkbenchError(f"Symlinks are not supported by M1: {name}")
        current = current.parent
    return result


def write_json(path: Path, value: dict, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # Never write secret-bearing state with a permissive initial mode.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if not private:
        temporary.chmod(0o600)
    temporary.replace(path)


def inventory(repo: Path, *, untracked: bool = True) -> dict:
    args = ["ls-files", "--cached", "-z"]
    if untracked:
        args += ["--others", "--exclude-standard"]
    names = set(git(repo, *args).decode().rstrip("\0").split("\0")) - {""}
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
        entries.append({"path": name, "sha256": digest, "executable": bool(after.st_mode & 0o111)})
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
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar:
                if excluded(member.name) or member.isdir():
                    continue
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
                path.write_bytes(content)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
        git(repo, "init", "-b", f"req/{requirement}")
        git(repo, "config", "user.name", "Agent Workbench Snapshot")
        git(repo, "config", "user.email", "snapshot@agent-workbench.invalid")
        git(repo, "add", "--all")
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
    manifest = json.loads((path / "workspace.json").read_text())
    if Path(manifest["repo"]).resolve() != path / "repo":
        raise WorkbenchError("Workspace manifest points outside its own repository")
    if (path / "repo").is_symlink():
        raise WorkbenchError("Workspace repository must not be a symlink")
    return path, manifest

