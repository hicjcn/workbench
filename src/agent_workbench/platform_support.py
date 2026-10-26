"""Host differences; Agent programs and their command syntax remain Linux based."""
import csv
import io
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

IS_WINDOWS = os.name == "nt"


def docker_argv(*args):
    # Never let source-controlled environment/context redirect execution remotely.
    endpoint = "npipe:////./pipe/dockerDesktopLinuxEngine" if IS_WINDOWS else "unix:///var/run/docker.sock"
    return ["docker", "--host=" + endpoint, *args]


def docker_environment(extra=None):
    environment = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(key, None)
    if extra:
        environment.update(extra)
    return environment


def container_ids():
    return (1000, 1000) if IS_WINDOWS else (os.getuid(), os.getgid())


def container_user():
    return ":".join(map(str, container_ids()))


def bind_mount(source, target, *, readonly=False):
    # Drive letters and spaces are a single argv value. CSV quoting also keeps
    # commas in host paths from introducing additional Docker mount options.
    value = str(source).replace("\\", "/") if IS_WINDOWS else str(source)
    if any(char in value + target for char in "\n\r\x00"):
        raise ValueError("Mount paths cannot contain control characters")
    fields = ["type=bind", "src=" + value, "dst=" + target]
    if readonly:
        fields.append("readonly")
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(fields)
    return output.getvalue()


def mount_matches(actual, expected):
    if not isinstance(actual, str):
        return False
    if not IS_WINDOWS:
        return actual == str(expected)
    expected = PureWindowsPath(expected)
    if not expected.is_absolute() or len(expected.drive) != 2 or ".." in expected.parts:
        return False  # UNC/WSL network shares are outside the native-host contract.
    candidate = PureWindowsPath(actual)
    if candidate.is_absolute() and len(candidate.drive) == 2:
        return candidate == expected and ".." not in candidate.parts
    for prefix in ("/run/desktop/mnt/host/", "/host_mnt/"):
        if actual.startswith(prefix):
            parts = PurePosixPath(actual[len(prefix):]).parts
            if not parts or len(parts[0]) != 1 or ".." in parts:
                return False
            return PureWindowsPath(parts[0] + ":/", *parts[1:]) == expected
    return False


def is_link(path):
    path = Path(path)
    if path.is_symlink():
        return True
    if IS_WINDOWS:
        try:
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except FileNotFoundError:
            pass
    return False
