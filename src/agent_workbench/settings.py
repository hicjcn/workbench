"""Single-host controller settings; secrets never enter API resource payloads."""
from dataclasses import dataclass
import os
from pathlib import Path
import secrets


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

    @classmethod
    def load(cls, data_dir: Path | None = None, *, sqlite: bool = False):
        root = (data_dir or Path(os.environ.get("AWB_DATA_DIR", ".local-data/workbench"))).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        values = []
        for name in ("access-token", "runner-token"):
            path = root / name
            if not path.exists():
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(secrets.token_urlsafe(32))
            if path.is_symlink():
                raise ValueError("Access token files must not be symbolic links")
            value = path.read_text().strip()
            if len(value) < 32:
                raise ValueError("Access token file is empty or invalid")
            values.append(value)
        local_database = f"sqlite:///{root / 'workbench.db'}"
        database_url = local_database if sqlite else os.environ.get("AWB_DATABASE_URL", "").strip() or local_database
        return cls(root, database_url,
                   *values, model=os.environ.get("AWB_MODEL", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"),
                   web_dir=Path(__file__).resolve().parents[2] / "web/dist",
                   max_active_jobs=positive_int("AWB_MAX_ACTIVE_JOBS", 2), max_containers=positive_int("AWB_MAX_CONTAINERS", 4))


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

