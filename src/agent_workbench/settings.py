"""Single-host controller settings; secrets never enter API resource payloads."""
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
from .platform_support import is_link


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    access_token: str
    runner_token: str
    model: str = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    lease_seconds: int = 45
    web_dir: Path | None = None
    max_active_jobs: int = 2
    max_containers: int = 4
    default_image: str = "agent-workbench-m1:local"
    port_range_start: int = 20000
    port_range_end: int = 29999

    @classmethod
    def load(cls, data_dir: Path | None = None, *, sqlite: bool = False, config_file=None):
        from .configuration import Configuration
        configuration = Configuration.load(config_file)
        root = (data_dir or configuration.data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        values = []
        for name in ("access-token", "runner-token"):
            path = root / name
            if not path.exists():
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(secrets.token_urlsafe(32))
            if is_link(path):
                raise ValueError("Access token files must not be symbolic links")
            value = path.read_text(encoding="utf-8").strip()
            if len(value) < 32:
                raise ValueError("Access token file is empty or invalid")
            values.append(value)
        local_database = f"sqlite:///{root / 'workbench.db'}"
        database_url = local_database if sqlite or configuration.use_sqlite else configuration.database_url or local_database
        return cls(root, database_url,
                   *values, model=configuration.model, lease_seconds=configuration.lease_seconds, web_dir=configuration.web_dir,
                   max_active_jobs=configuration.max_active_jobs, max_containers=configuration.max_containers, default_image=configuration.tool_image,
                   port_range_start=configuration.port_range_start, port_range_end=configuration.port_range_end)


def positive_int(name, default):
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= 32: raise ValueError(f"{name} must be between 1 and 32")
    return value


def storage_summary(database_url):
    """Only expose storage type and a local path, never a server connection URL."""
    from sqlalchemy.engine import make_url
    url = make_url(database_url)
    backend = url.get_backend_name()
    local = backend == "sqlite"
    path = url.database if local else None
    return {"backend": backend, "path": path, "mode": "memory" if local and path in (None, "", ":memory:") else "file" if local else "server",
        "external_service_required": not local}

