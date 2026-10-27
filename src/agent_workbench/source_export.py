"""Materialize committed, repository-local links without creating filesystem links."""
import io
import ntpath
from pathlib import PurePosixPath
import tarfile

from .platform_support import IS_WINDOWS
from .workspace import WorkbenchError, excluded, safe_path, windows_names

LIMIT = 100 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_LINKS = 40


def export(archive, repo):
    entries, directories, children = {}, {""}, {}
    executable, links, cases = [], [], {}
    total, count = 0, 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            name = member.name.rstrip("/") if member.isdir() else member.name
            if excluded(name):
                continue
            safe_path(repo, name)
            if name != str(PurePosixPath(name)) or name in entries:
                raise WorkbenchError(f"源码归档包含重复或不规范的路径：{name}")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise WorkbenchError(f"源码归档包含不支持的文件类型：{name}")
            entries[name] = member
            if len(entries) > MAX_ENTRIES:
                raise WorkbenchError("源码导出条目超过 100000 项限制")
            parts = PurePosixPath(name).parts
            for length in range(1, len(parts) + 1):
                parent, child = "/".join(parts[:length - 1]), "/".join(parts[:length])
                children.setdefault(parent, set()).add(child)
                if length < len(parts) or member.isdir():
                    directories.add(child)
            if member.issym():
                links.append({"path": name, "target": member.linkname})
        for name in directories - {""}:
            if name in entries and not entries[name].isdir():
                raise WorkbenchError(f"源码归档的文件与目录冲突：{name}")

        def resolve(parts, base=(), chain=()):
            resolved = list(base)
            for part in parts:
                if part in ("", "."):
                    continue
                if "/".join(resolved) not in directories:
                    raise WorkbenchError("源码链接经过了非目录目标")
                if part == "..":
                    if not resolved:
                        raise WorkbenchError("源码链接指向仓库外；请使用项目运行配置的挂载接入外部目录")
                    resolved.pop()
                    continue
                name = "/".join([*resolved, part])
                if excluded(name):
                    raise WorkbenchError(f"源码链接指向已排除的内容：{name}")
                member = entries.get(name)
                if member and member.issym():
                    if name in chain or len(chain) >= MAX_LINKS:
                        raise WorkbenchError(f"源码链接形成循环或超过 {MAX_LINKS} 层：{name}")
                    target = member.linkname
                    if not target or "\x00" in target or "\\" in target or ntpath.splitdrive(target)[0] or PurePosixPath(target).is_absolute():
                        raise WorkbenchError(f"源码链接 {name} 的目标必须是仓库内相对路径（当前：{target[:240]}）；外部目录请通过项目运行配置挂载")
                    try:
                        resolved = list(resolve(PurePosixPath(target).parts, tuple(resolved), (*chain, name)))
                    except WorkbenchError as error:
                        raise WorkbenchError(f"链接 {name} → {target[:240]}：{error}") from error
                elif name in directories or member and member.isfile():
                    resolved.append(part)
                else:
                    raise WorkbenchError(f"源码链接目标未包含在已提交的导出内容中：{name}")
            return tuple(resolved)

        pending = [("", "", frozenset())]
        while pending:
            source, output, ancestors = pending.pop()
            try:
                resolved = "/".join(resolve(PurePosixPath(source).parts))
            except WorkbenchError as error:
                raise WorkbenchError(f"无法展开源码条目 {output or source}：{error}") from error
            if output:
                count += 1
                if count > MAX_ENTRIES:
                    raise WorkbenchError("展开链接后的源码超过 100000 条目限制")
            if resolved in directories:
                if resolved in ancestors:
                    raise WorkbenchError(f"源码目录链接形成循环：{output}")
                if output:
                    if IS_WINDOWS:
                        windows_names([output], cases)
                    safe_path(repo, output).mkdir(parents=True, exist_ok=True)
                for child in sorted(children.get(resolved, ()), reverse=True):
                    name = child.rsplit("/", 1)[-1]
                    target = f"{output}/{name}" if output else name
                    # Apply exclusions to both original and materialized paths.
                    if not excluded(target):
                        pending.append((child, target, ancestors | {resolved}))
                continue
            member = entries[resolved]
            if excluded(output):
                continue
            if IS_WINDOWS:
                windows_names([output], cases)
            path = safe_path(repo, output)
            total += member.size
            if total > LIMIT:
                raise WorkbenchError("展开链接后的源码超过 100 MiB 限制")
            stream = tar.extractfile(member)
            if stream is None:
                raise WorkbenchError(f"源码归档条目无法读取：{resolved}")
            with stream:
                content = stream.read()
            if content.startswith((b"version https://git-lfs.github.com/spec/v1\n", b"version https://git-lfs.github.com/spec/v1\r\n")):
                raise WorkbenchError("Git LFS pointers are not materialized by M1")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
            if member.mode & 0o111:
                executable.append(output)
    return executable, links
