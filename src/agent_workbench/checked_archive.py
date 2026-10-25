"""Private bounded ZIP archives with fixed paths and per-entry integrity."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile

from .workspace import WorkbenchError

LIMIT = 512 * 1024 * 1024


def name_safe(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or name != str(path):
        raise WorkbenchError("归档包含不安全路径")
    return name


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""): digest.update(piece)
    return digest.hexdigest()


def build(destination, header, entries, limit=LIMIT):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() or destination.is_symlink() or destination.parent.is_symlink(): raise WorkbenchError("归档已存在或存储路径不可信")
    fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=destination.parent)
    recorded, seen, total = [], set(), 0
    try:
        with os.fdopen(fd, "w+b") as output, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for name, source in entries:
                name_safe(name)
                if name in seen or name == "manifest.json" or len(seen) >= 50000: raise WorkbenchError("归档条目重复或数量超过限制")
                seen.add(name)
                mode, before = 0o600, None
                if isinstance(source, Path):
                    if source.is_symlink() or not source.is_file(): raise WorkbenchError("归档不接受符号链接或特殊文件")
                    before = source.stat(); size = before.st_size; mode = 0o755 if before.st_mode & 0o111 else 0o644
                else: size = len(source)
                total += size
                if total > limit: raise WorkbenchError("归档超过容量上限")
                info = zipfile.ZipInfo(name); info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                checksum = hashlib.sha256()
                with archive.open(info, "w", force_zip64=True) as target:
                    if isinstance(source, Path):
                        with source.open("rb") as stream:
                            for piece in iter(lambda: stream.read(1024 * 1024), b""):
                                checksum.update(piece); target.write(piece)
                    else: checksum.update(source); target.write(source)
                if before:
                    after = source.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns): raise WorkbenchError("复制时文件发生变化")
                recorded.append({"path": name, "size": size, "sha256": checksum.hexdigest(), "mode": mode})
            manifest = {**header, "entries": recorded}
            body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
            if len(body) > 16 * 1024 * 1024: raise WorkbenchError("归档清单超过限制")
            archive.writestr("manifest.json", body)
        os.link(temporary, destination)
    finally: Path(temporary).unlink(missing_ok=True)
    return {"sha256": file_hash(destination), "size": destination.stat().st_size,
        "manifest": {**header, "entry_count": len(recorded), "manifest_sha256": hashlib.sha256(body).hexdigest()}}


def verify(path, expected=None, limit=LIMIT):
    try:
        if path.is_symlink() or path.stat().st_size > limit + 20 * 1024 * 1024: raise WorkbenchError("归档文件不可信或过大")
        if expected and (path.stat().st_size != expected["size"] or file_hash(path) != expected["sha256"]): raise WorkbenchError("归档文件摘要不符")
        with zipfile.ZipFile(path) as archive:
            if archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024: raise WorkbenchError("归档清单过大")
            body = archive.read("manifest.json"); manifest = json.loads(body)
            entries = manifest["entries"]
            if len(entries) > 50000 or sum(item["size"] for item in entries) > limit: raise WorkbenchError("归档展开大小超过限制")
            names = [item["path"] for item in entries]
            if len(set(names)) != len(names) or len(archive.namelist()) != len(names) + 1 or set(archive.namelist()) != {*names, "manifest.json"}: raise WorkbenchError("归档条目与清单不一致")
            for item in entries:
                name_safe(item["path"])
                info = archive.getinfo(item["path"])
                if info.file_size != item["size"] or item["mode"] not in (0o600, 0o644, 0o755) or stat.S_ISLNK(info.external_attr >> 16): raise WorkbenchError("归档条目类型或大小不符")
                checksum = hashlib.sha256()
                with archive.open(info) as stream:
                    for piece in iter(lambda: stream.read(1024 * 1024), b""): checksum.update(piece)
                if checksum.hexdigest() != item["sha256"]: raise WorkbenchError("归档条目摘要不符")
            summary = {key: value for key, value in manifest.items() if key != "entries"}
            summary.update(entry_count=len(entries), manifest_sha256=hashlib.sha256(body).hexdigest())
            if expected and summary != expected["manifest"]: raise WorkbenchError("归档清单摘要不符")
            return manifest
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
        raise WorkbenchError("归档已损坏、丢失或格式不支持") from None
