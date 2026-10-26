"""One UTF-8 TOML file, with explicit per-setting environment overrides."""
from dataclasses import dataclass, field
import json
import hashlib
import math
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlparse

from .platform_support import checked_docker_endpoint

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"


class ConfigurationError(ValueError):
    pass


def config_path(value=None):
    selected = str(value) if value is not None else os.environ.get("AWB_CONFIG_FILE")
    if selected == "-":
        return None
    return Path(selected or "workbench.toml").expanduser().resolve()


def launcher_directory(path):
    key = str(path) if path else "environment:" + str(Path.cwd().resolve())
    base = path.parent if path else Path.cwd()
    return base / ".workbench-run" / hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Configuration:
    file: Path | None
    data_dir: Path
    web_dir: Path
    venv_dir: Path
    logs_dir: Path
    tool_context: Path
    model: str = DEFAULT_MODEL
    database_url: str = field(default="", repr=False)
    use_sqlite: bool = False
    port: int = 8340
    api_url: str = "http://127.0.0.1:8340"
    max_active_jobs: int = 2
    max_containers: int = 4
    lease_seconds: int = 45
    task_timeout_seconds: float = 300
    proxy_ca: Path | None = None
    docker_endpoint: str = ""
    startup_timeout_seconds: int = 90
    stop_timeout_seconds: int = 120
    graceful_timeout_seconds: int = 10
    image_build_timeout_seconds: int = 900
    open_browser: bool = True
    require_docker: bool = True
    stop_managed_environments: bool = True
    tool_image: str = "agent-workbench-m1:local"
    environment: dict = field(default_factory=dict, repr=False)
    agent_cli: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, file=None):
        path = config_path(file)
        explicit = file is not None or bool(os.environ.get("AWB_CONFIG_FILE"))
        document = {}
        if path and path.exists():
            try:
                document = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                raise ConfigurationError("配置文件无法读取或 TOML 格式无效；未输出文件内容") from None
        elif path and explicit:
            raise ConfigurationError("指定的配置文件不存在")
        base = path.parent if path and path.exists() else Path.cwd()
        unknown = set(document) - {"workbench", "launcher", "environment", "agent_cli"}
        if unknown:
            raise ConfigurationError("配置文件包含不支持的配置节")
        allowed_workbench = {"data_dir", "web_dir", "model", "database_url", "use_sqlite", "port", "api_url",
            "max_active_jobs", "max_containers", "lease_seconds", "task_timeout_seconds", "proxy_ca", "docker_endpoint"}
        allowed_launcher = {"venv_dir", "logs_dir", "tool_context", "tool_image", "startup_timeout_seconds",
            "stop_timeout_seconds", "graceful_timeout_seconds", "image_build_timeout_seconds", "open_browser", "require_docker", "stop_managed_environments"}
        for section, allowed in (("workbench", allowed_workbench), ("launcher", allowed_launcher)):
            if not isinstance(document.get(section, {}), dict) or set(document.get(section, {})) - allowed:
                raise ConfigurationError(section + " 配置节包含不支持的字段")
        values = {**document.get("workbench", {}), **document.get("launcher", {})}
        environment = document.get("environment", {})
        if not isinstance(environment, dict):
            raise ConfigurationError("environment 必须是变量名和值的配置表")
        for name, value in environment.items():
            credential = name.endswith(("_API_KEY", "_TOKEN", "_BASE_URL")) or name.startswith("AGENT_")
            if (not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name) or not isinstance(value, str) or "\x00" in value or
                name.startswith(("AWB_", "DOCKER_", "LD_", "PYTHON", "OPENCODE_SERVER_")) or
                not (credential or name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "PIP_CERT", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "GIT_SSL_CAINFO", "REQUESTS_CA_BUNDLE"))):
                raise ConfigurationError("environment 只接受模型/CLI 凭据、HTTP 代理和 CA 变量；运行参数使用对应配置字段")

        def value(name, default):
            if name == "task_timeout_seconds" and "AWB_TASK_TIMEOUT_SECONDS" not in os.environ and "AWB_TASK_TIMEOUT" in os.environ:
                return os.environ["AWB_TASK_TIMEOUT"]
            return os.environ.get("AWB_" + name.upper(), values.get(name, default))

        def text(name, default=""):
            selected = value(name, default)
            if not isinstance(selected, str) or "\x00" in selected:
                raise ConfigurationError(name + " 必须是文本")
            return selected.strip()

        def number(name, default, low, high, *, integer=True):
            selected = value(name, default)
            try:
                if isinstance(selected, bool): raise ValueError
                result = int(selected) if integer else float(selected)
                if not math.isfinite(result) or not low <= result <= high or (integer and isinstance(selected, float) and selected != result): raise ValueError
                return result
            except (TypeError, ValueError, OverflowError):
                raise ConfigurationError(name + " 数值或范围无效") from None

        def boolean(name, default):
            selected = value(name, default)
            if isinstance(selected, bool): return selected
            if isinstance(selected, str) and selected.strip().lower() in ("1", "true", "yes", "0", "false", "no"):
                return selected.strip().lower() in ("1", "true", "yes")
            raise ConfigurationError(name + " 必须是布尔值")

        def location(name, default):
            selected = text(name, str(default))
            if not selected: raise ConfigurationError(name + " 不能为空")
            raw = Path(selected).expanduser()
            origin = Path.cwd() if "AWB_" + name.upper() in os.environ else base
            return (raw if raw.is_absolute() else origin / raw).resolve()

        port = number("port", 8340, 1, 65535)
        api_url = text("api_url") or f"http://127.0.0.1:{port}"
        parsed = urlparse(api_url)
        try:
            valid_api = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ("", "/")
        except ValueError:
            valid_api = False
        if not valid_api:
            raise ConfigurationError("api_url 必须是本机回环 HTTP 根地址，且包含端口")
        proxy = os.environ.get("AWB_PROXY_CA") or os.environ.get("CODEX_PROXY_CERT") or text("proxy_ca")
        proxy_path = None
        if proxy:
            raw = Path(proxy).expanduser()
            origin = Path.cwd() if "AWB_PROXY_CA" in os.environ or "CODEX_PROXY_CERT" in os.environ else base
            proxy_path = (raw if raw.is_absolute() else origin / raw).resolve()
        endpoint = text("docker_endpoint")
        try:
            checked_docker_endpoint(endpoint)
        except ValueError:
            raise ConfigurationError("docker_endpoint 只允许本机 Docker Unix socket 或 Docker Desktop Linux 管道") from None
        image = text("tool_image", "agent-workbench-m1:local")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", image) or "://" in image or ("@" in image and not re.fullmatch(r"[^@]+@sha256:[a-f0-9]{64}", image)):
            raise ConfigurationError("tool_image 不是有效的镜像引用")
        agent_cli = document.get("agent_cli", {})
        if not isinstance(agent_cli, dict):
            raise ConfigurationError("agent_cli 必须是配置表")
        allowed_agent = {"enabled", "name", "executable", "expected_version", "timeout_seconds", "credential_env", "serve_command", "run_command", "version_command"}
        if set(agent_cli) - allowed_agent:
            raise ConfigurationError("agent_cli 包含不支持的字段；凭据值请放在 environment 或进程环境中")
        agent_cli = dict(agent_cli)
        for name in allowed_agent:
            selected = os.environ.get("AWB_AGENT_CLI_" + name.upper())
            if selected is not None:
                if name == "credential_env": selected = [item.strip() for item in selected.split(",") if item.strip()]
                elif name == "enabled":
                    if selected.lower() not in ("true", "false", "1", "0", "yes", "no"): raise ConfigurationError("agent_cli.enabled 必须是布尔值")
                    selected = selected.lower() in ("true", "1", "yes")
                elif name == "timeout_seconds":
                    try: selected = int(selected)
                    except ValueError: raise ConfigurationError("agent_cli.timeout_seconds 无效") from None
                agent_cli[name] = selected
        if not isinstance(agent_cli.get("enabled", False), bool): raise ConfigurationError("agent_cli.enabled 必须是布尔值")
        if any(not isinstance(selected, str) for name, selected in agent_cli.items() if name not in ("enabled", "timeout_seconds", "credential_env")):
            raise ConfigurationError("agent_cli 名称和命令必须是文本")
        timeout = agent_cli.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ConfigurationError("agent_cli.timeout_seconds 必须在 1 到 3600 之间")
        names = agent_cli.get("credential_env", [])
        if not isinstance(names, list) or not all(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name) for name in names):
            raise ConfigurationError("agent_cli.credential_env 必须是变量名数组，不包含实际值")
        model = text("model", DEFAULT_MODEL)
        if not (re.fullmatch(r"opencode/(?:[A-Za-z0-9._-]+-free|big-pickle)", model) or re.fullmatch(r"openrouter/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+:free", model)):
            raise ConfigurationError("model 必须是当前已适配的显式免费模型标识")
        return cls(file=path, data_dir=location("data_dir", ".local-data/workbench"), web_dir=location("web_dir", PROJECT_ROOT / "web/dist"),
            venv_dir=location("venv_dir", ".venv"), logs_dir=location("logs_dir", ".local-data/logs"), tool_context=location("tool_context", PROJECT_ROOT / "poc/container"),
            model=model, database_url=text("database_url"), use_sqlite=boolean("use_sqlite", False), port=port, api_url=api_url.rstrip("/"),
            max_active_jobs=number("max_active_jobs", 2, 1, 32), max_containers=number("max_containers", 4, 1, 32), lease_seconds=number("lease_seconds", 45, 10, 3600),
            task_timeout_seconds=number("task_timeout_seconds", 300, 0.001, 86400, integer=False), proxy_ca=proxy_path, docker_endpoint=endpoint,
            startup_timeout_seconds=number("startup_timeout_seconds", 90, 5, 600), stop_timeout_seconds=number("stop_timeout_seconds", 120, 5, 3600),
            graceful_timeout_seconds=number("graceful_timeout_seconds", 10, 1, 120), image_build_timeout_seconds=number("image_build_timeout_seconds", 900, 10, 3600),
            open_browser=boolean("open_browser", True), require_docker=boolean("require_docker", True), stop_managed_environments=boolean("stop_managed_environments", True),
            tool_image=image, environment=dict(environment), agent_cli=dict(agent_cli))

    def apply(self):
        """Entry points export resolved settings once; reading a file is side-effect free."""
        for name, value in self.environment.items():
            os.environ.setdefault(name, value)
        for name in ("data_dir", "web_dir", "venv_dir", "logs_dir", "tool_context", "model", "database_url", "use_sqlite", "port", "api_url", "max_active_jobs", "max_containers",
            "lease_seconds", "task_timeout_seconds", "docker_endpoint", "startup_timeout_seconds", "stop_timeout_seconds", "graceful_timeout_seconds", "image_build_timeout_seconds", "open_browser", "require_docker", "stop_managed_environments", "tool_image"):
            value = getattr(self, name)
            os.environ["AWB_" + name.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
        if self.proxy_ca:
            os.environ["CODEX_PROXY_CERT"] = str(self.proxy_ca)
        os.environ["AWB_TASK_TIMEOUT"] = str(self.task_timeout_seconds)
        for name, value in self.agent_cli.items():
            if name == "credential_env": value = ",".join(value)
            os.environ["AWB_AGENT_CLI_" + name.upper()] = str(value).lower() if isinstance(value, bool) else str(value)

    def public(self):
        return {"config_file": str(self.file) if self.file else None, "data_dir": str(self.data_dir), "web_dir": str(self.web_dir), "venv_dir": str(self.venv_dir),
            "logs_dir": str(self.logs_dir), "model": self.model, "port": self.port, "api_url": self.api_url, "require_docker": self.require_docker,
            "stop_managed_environments": self.stop_managed_environments, "docker_endpoint": checked_docker_endpoint(self.docker_endpoint),
            "tool_context": str(self.tool_context), "tool_image": self.tool_image}


def configured(args):
    result = Configuration.load(getattr(args, "config", None))
    result.apply()
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        print(json.dumps(Configuration.load(args.config).public(), ensure_ascii=False))
    except ConfigurationError as error:
        parser.exit(1, str(error) + "\n")
