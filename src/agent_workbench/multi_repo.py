"""Explicit repository sets in a shared parent; metadata stays outside Agent mounts."""
import hashlib
import errno
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

from .platform_support import IS_WINDOWS, is_link
from .workspace import WorkbenchError, excluded, git, safe_path, write_json

MAX_REPOSITORIES = 16
LIMIT = 100 * 1024 * 1024


def relative_names(values, *, maximum=MAX_REPOSITORIES):
    if not values or len(values) > maximum:
        raise WorkbenchError(f"请选择 1 至 {maximum} 个路径")
    result = []
    for value in values:
        path = PurePosixPath(value)
        if (value != str(path) or len(value) > 240 or excluded(value) or
                any(char in value for char in "\\\n\r\x00") or value.startswith("-")):
            raise WorkbenchError("请使用父目录内规范的相对路径")
        safe_path(Path('/__awb_relative_path_validation__'), value)
        result.append(value)
    folded = sorted(value.casefold() for value in result)
    if len(set(folded)) != len(folded) or any(a != b and b.startswith(a + '/') for a in folded for b in folded):
        raise WorkbenchError("路径不能重复、大小写冲突或互相嵌套")
    return result


def source_root(raw):
    path = Path(raw).expanduser()
    if not path.is_absolute(): raise WorkbenchError("请填写本机绝对目录")
    current = path
    while True:
        if is_link(current): raise WorkbenchError("源码路径不能经过符号链接或 junction")
        if current.parent == current: break
        current = current.parent
    path = path.resolve(strict=True)
    if not path.is_dir(): raise WorkbenchError("源码目录不存在")
    return path


def validate_sources(root, repositories, shared_files=()):
    repositories = relative_names(repositories)
    shared_files = relative_names(shared_files, maximum=32) if shared_files else []
    # An ancestor repository does not make this parent a repository root.
    try:
        enclosing = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    except WorkbenchError:
        pass
    else:
        if enclosing == root:
            raise WorkbenchError("多仓库模式请填写各 Git 仓库之外的共同父目录")
    for name in repositories:
        path = safe_path(root, name)
        if not path.is_dir() or Path(git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve() != path.resolve():
            raise WorkbenchError(f"请选择独立 Git 仓库根目录：{name}")
        git(path, "rev-parse", "--verify", "HEAD^{commit}")
    for name in shared_files:
        if any(name.casefold() == repo.casefold() or name.casefold().startswith(repo.casefold() + '/') for repo in repositories):
            raise WorkbenchError("共享文件必须位于候选仓库之外")
        path = safe_path(root, name)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise WorkbenchError("共享文件必须是最多 1 MiB 的普通文件")
    return repositories, shared_files


def discover(raw, data_dir):
    root = source_root(raw)
    if data_dir.resolve().is_relative_to(root): raise WorkbenchError("父目录不能包含工作台数据目录")
    found, shared, visited, limited = [], [], 0, False
    def scan(directory, depth):
        nonlocal visited, limited
        if visited >= 2048 or len(found) >= 128:
            limited = True; return
        visited += 1
        if (directory / '.git').exists():
            if directory == root: raise WorkbenchError("这是单个 Git 仓库，请使用单仓库模式登记")
            try:
                sha = git(directory, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
                if Path(git(directory, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != directory: return
                found.append({'path': directory.relative_to(root).as_posix(), 'commit_sha': sha})
            except WorkbenchError:
                return
            return
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if is_link(path) or excluded(relative): continue
            if path.is_dir():
                if depth < 4: scan(path, depth + 1)
                else: limited = True
            elif directory == root and path.is_file() and path.suffix.lower() in ('.md', '.txt') and path.stat().st_size <= 1024 * 1024:
                shared.append(relative)
    scan(root, 0)
    return {'source_path': str(root), 'repositories': found, 'shared_files': shared, 'limited': limited, 'max_repositories': MAX_REPOSITORIES}


def layout(repo):
    if repo.name != 'repo': return None
    path = repo.parent / 'workspace.json'
    if not path.exists(): return None
    if is_link(path) or path.stat().st_size > 2 * 1024 * 1024: raise WorkbenchError("工作区清单不可信")
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('layout') != 'multi-repo/v1': return None
    names = relative_names([item['path'] for item in value['repositories']])
    for name in names: safe_path(repo, name)
    return value


def common_inventory(repo, names, executable=None):
    entries = []
    def scan(directory):
        for path in sorted(directory.iterdir()):
            name = path.relative_to(repo).as_posix()
            if name in names: continue
            if path.name == '.git': raise WorkbenchError("共同父目录及共享目录不能新增 Git 元数据")
            if excluded(name): continue
            path = safe_path(repo, name)
            if path.is_dir(): scan(path)
            elif path.is_file():
                before = path.stat(); data = path.read_bytes(); after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise WorkbenchError("共享文件在快照期间变化")
                mode = bool(after.st_mode & 0o111) if not IS_WINDOWS else (executable or {}).get(name, False)
                entries.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'executable': mode})
            else: raise WorkbenchError("共享目录包含不支持的文件类型")
    scan(repo)
    return entries


def combine(value, common, snapshots):
    """Compose the existing public digest format from independently verified trees."""
    files = list(common)
    repositories = []
    for item in value['repositories']:
        current = snapshots[item['path']]
        repositories.append({'path': item['path'], 'source_commit': item['source_commit'], 'commit_sha': current['commit_sha'],
            'tree_digest': current['tree_digest'], 'file_count': len(current['files']),
            'changed': current['tree_digest'] != item['baseline']['tree_digest']})
        files.extend({**entry, 'path': item['path'] + '/' + entry['path']} for entry in current['files'])
    files.sort(key=lambda item: item['path'])
    tree = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    heads = [(item['path'], item['commit_sha']) for item in repositories]
    return {'commit_sha': hashlib.sha256(json.dumps(heads).encode()).hexdigest(), 'tree_digest': tree, 'files': files, 'repositories': repositories}


def capture(repo, value):
    from .workspace import inventory
    names = [item['path'] for item in value['repositories']]
    common_modes = {item['path']: item.get('executable', False) for item in value.get('common_baseline', [])}
    common = common_inventory(repo, names, common_modes)
    snapshots = {}
    for item in value['repositories']:
        child = safe_path(repo, item['path'])
        snapshots[item['path']] = {**inventory(child), 'commit_sha': git(child, 'rev-parse', 'HEAD').decode().strip()}
    result = combine(value, common, snapshots)
    if sum(safe_path(repo, item['path']).stat().st_size for item in result['files']) > LIMIT:
        raise WorkbenchError("多仓库源码合计超过 100 MiB 限制")
    return result


def snapshot(repo, value):
    first, second = capture(repo, value), capture(repo, value)
    if first != second: raise WorkbenchError("多仓库代码在快照期间变化，请停止写入后重试")
    return first


def create(source, root, requirement, project, *, repositories, shared_files, include_dirty=False, include_untracked=False):
    from .workspace import create_workspace
    from . import logging_support as logs
    existing = layout(source)
    if existing:
        repositories = [item['path'] for item in existing['repositories']]
        with logs.operation('workspace', 'source.multi_snapshot', '核对多仓库副本来源'):
            before = snapshot(source, existing)
        shared_files = [item['path'] for item in common_inventory(source, repositories,
            {item['path']: item.get('executable', False) for item in existing.get('common_baseline', [])})]
    else:
        repositories, shared_files = validate_sources(source, repositories, shared_files)
        before = None
    source_heads = {name: git(safe_path(source, name), 'rev-parse', 'HEAD').decode().strip() for name in repositories}
    destination = safe_path(root, f'{requirement}/{project}')
    if destination.exists(): raise WorkbenchError("Workspace already exists; it will not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f'.{project}-', dir=destination.parent))
    staging = None
    try:
        # Keep the inner Git export out of the deeply nested requirement path.
        # A configured data drive can differ from the system temporary drive.
        staging = tempfile.TemporaryDirectory(prefix='awb-repositories-')
        repo = temporary / 'repo'; repo.mkdir()
        value = {'schema_version': 2, 'layout': 'multi-repo/v1', 'requirement': requirement, 'project': project,
            'repo': str(destination / 'repo'), 'source_history': 'independent_snapshot_exports', 'repositories': [],
            'include_dirty': include_dirty, 'include_untracked': include_untracked}
        copied_snapshots, baselines = {}, {}
        for index, name in enumerate(repositories):
            logs.event("workspace", "source.repository", "正在复制子仓库", detailed=True, repository=name, count=index+1, total=len(repositories))
            try:
                copied = create_workspace(safe_path(source, name), Path(staging.name), requirement, f'repository_{index}',
                    include_dirty=include_dirty, include_untracked=include_untracked, _single_repository=True)
            except WorkbenchError as error:
                raise WorkbenchError(f"复制子仓库 {name} 失败：{error}") from error
            child = Path(copied['workspace'])
            manifest = json.loads((child / 'workspace.json').read_text(encoding='utf-8'))
            copied_snapshots[name], baselines[name] = copied['snapshot'], manifest['source_baseline']
            if manifest['source_commit'] != source_heads[name]: raise WorkbenchError('来源仓库在准备期间切换了提交，请重试')
            target = safe_path(repo, name); target.parent.mkdir(parents=True, exist_ok=True)
            try: (child / 'repo').rename(target)
            except OSError as error:
                if error.errno != errno.EXDEV and getattr(error, 'winerror', None) != 17: raise
                shutil.copytree(child / 'repo', target)
            value['repositories'].append({'path': name, 'source_commit': manifest['source_commit'], 'baseline': manifest['source_baseline'],
                **({'materialized_links': manifest['materialized_links']} if manifest.get('materialized_links') else {}),
                **({'copied_link_files': manifest['copied_link_files']} if manifest.get('copied_link_files') else {})})
            logs.event("workspace", "source.repository_ready", "子仓库副本已完成", detailed=True, repository=name, count=index+1, total=len(repositories))
        shared_base = temporary / 'shared-base'; shared_base.mkdir()
        for name in shared_files:
            origin = safe_path(source, name)
            data = origin.read_bytes(); stat = origin.stat()
            for folder in (repo, shared_base):
                target = safe_path(folder, name); target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data); target.chmod(stat.st_mode & 0o777)
            if hashlib.sha256(origin.read_bytes()).digest() != hashlib.sha256(data).digest():
                raise WorkbenchError("共享文件在复制期间变化")
        value['common_baseline'] = common_inventory(repo, repositories,
            {item['path']: item.get('executable', False) for item in existing.get('common_baseline', [])} if existing else None)
        write_json(temporary / 'workspace.json', value)
        # Each private child was already verified before moving it here. Keep its
        # committed baseline separate from dirty contents without rescanning both.
        value['source_baseline'] = combine(value, value['common_baseline'], baselines)
        value['source_commit'] = value['source_baseline']['commit_sha']
        write_json(temporary / 'workspace.json', value)
        with logs.operation('workspace', 'source.verify_copy', '核对已复制的多仓库源码'):
            observed = capture(repo, value)
            expected = combine(value, value['common_baseline'], copied_snapshots)
            if observed != expected: raise WorkbenchError('多仓库副本与已核实的源码不一致')
        if before and snapshot(source, existing) != before: raise WorkbenchError("来源多仓库工作区在复制期间变化")
        if any(git(safe_path(source, name), 'rev-parse', 'HEAD').decode().strip() != sha for name, sha in source_heads.items()):
            raise WorkbenchError('来源仓库在准备期间切换了提交，请重试')
        temporary.rename(destination)
        return {'workspace': str(destination), 'source_commit': value['source_commit'], 'snapshot': observed}
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if staging: staging.cleanup()


def diff(workspace, *, repository=None):
    from .workspace import binary_diff
    value = layout(workspace / 'repo')
    if not value: raise WorkbenchError("需要多仓库工作区")
    chunks = []
    for item in value['repositories']:
        if repository is not None and repository != item['path']: continue
        child = safe_path(workspace / 'repo', item['path'])
        from .workspace import snapshot as child_snapshot
        chunks.append(binary_diff(child, item['baseline'], child_snapshot(child, single_repository=True)['files'], prefix='' if repository else item['path'] + '/'))
    if repository is not None: return b''.join(chunks)
    # Both sides use fixed directory names, producing portable a/path -> b/path patches.
    with tempfile.TemporaryDirectory(prefix='.common-diff-', dir=workspace) as temporary:
        folder = Path(temporary); (folder / 'a').mkdir(); (folder / 'b').mkdir()
        for path in checked_shared_base(workspace, value):
            name = path.relative_to(workspace / 'shared-base').as_posix()
            path = safe_path(workspace / 'shared-base', name)
            if path.is_file():
                target = safe_path(folder / 'a', name); target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)
        for item in common_inventory(workspace / 'repo', [entry['path'] for entry in value['repositories']]):
            target = safe_path(folder / 'b', item['path']); target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(workspace / 'repo', item['path']), target)
        result = subprocess.run(['git', '-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false', '-c', 'core.autocrlf=false',
            *(['-c', 'core.longpaths=true'] if IS_WINDOWS else []), 'diff', '--no-index',
            '--no-ext-diff', '--no-textconv', '--binary', '--src-prefix=', '--dst-prefix=', '--', 'a', 'b'], cwd=folder, capture_output=True, timeout=120)
        if result.returncode not in (0, 1): raise WorkbenchError("共享文件差异采集失败")
        chunks.append(result.stdout)
    return b''.join(chunks)


def checked_shared_base(workspace, value):
    root = workspace / 'shared-base'
    expected = {item['path']: item['sha256'] for item in value['common_baseline']}
    files = []
    if is_link(root): raise WorkbenchError('共享文件基线不可信')
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        path = safe_path(root, name)
        if path.is_file():
            if name not in expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected.pop(name):
                raise WorkbenchError('共享文件基线摘要不符')
            files.append(path)
    if expected: raise WorkbenchError('共享文件基线缺失')
    return files
