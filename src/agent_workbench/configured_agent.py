"""Optional startup preset, saved through the same immutable CLI version service."""
import re

from .agent_clis import Content, Configurations, Save, digest
from .configuration import ConfigurationError
from .store import Conflict, Store


def apply_preset(configuration, settings):
    values = configuration.agent_cli
    if not values.get("enabled", False): return None
    program = values.get("executable", "opencode")
    if not isinstance(program, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", program):
        raise ConfigurationError("agent_cli.executable 必须是镜像中的单个程序名或路径")
    try:
        content = Content(serve_command=values.get("serve_command", program + " serve --hostname {host} --port {port}"),
            run_command=values.get("run_command", program + " run --attach {endpoint} --model {model} --format json"),
            version_command=values.get("version_command", program + " --version"), expected_version=values.get("expected_version", ""),
            timeout_seconds=values.get("timeout_seconds", 300), credential_env=values.get("credential_env", [])).model_dump()
        desired = Save(name=values.get("name", program), content=content, actor="启动配置", reason="从统一配置预置新工作区默认 CLI", set_default=True)
    except (ValueError, TypeError):
        raise ConfigurationError("agent_cli 配置无效；请检查命令、超时和凭据变量名，未输出字段值") from None
    store = Store(settings.database_url, settings.lease_seconds, settings.max_active_jobs)
    try:
        service = Configurations(store)
        state = service.read()
        matches = [item for item in state["configs"] if item["name"] == desired.name and item["content_digest"] == digest(content)]
        if matches:
            if state["default_id"] != matches[0]["id"]: service.activate(matches[0]["id"])
            return None
        managed = [item for item in state["configs"] if item["name"] == desired.name and item["actor"] == "启动配置"]
        family = max(managed, key=lambda item: item["number"])["family"] if managed else None
        latest = max((item for item in state["configs"] if item.get("family") == family), key=lambda item: item["number"], default=None) if family else None
        service.save({**desired.model_dump(), "expected_config": latest["id"] if latest else None})
        return None
    except Conflict:
        return "Agent CLI 预置暂未应用：请先核实活动/未知镜像任务，再重启；界面仍可用于核实"
    finally:
        store.engine.dispose()
