import argparse
import os
from pathlib import Path
import threading

from .settings import Settings
from . import logging_support as logs


def migrate(settings):
    from alembic.config import Config
    from alembic import command
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def main(stop_event=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config", help="统一 TOML 配置文件；- 表示只使用环境变量")
    parser.add_argument("--port", type=int)
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--sqlite", action="store_true", help="使用数据目录中的本地 workbench.db，忽略 AWB_DATABASE_URL")
    args = parser.parse_args()
    from .configuration import configured
    configuration = configured(args)
    settings = Settings.load(args.data_dir, sqlite=args.sqlite, config_file=args.config)
    logs.configure(configuration.log_mode, secrets=(settings.access_token, settings.runner_token))
    logs.event("api", "api.start", "正在启动 API", mode=configuration.log_mode, pid=os.getpid())
    from .resources import controller_lock
    with controller_lock(settings.data_dir):
        with logs.operation("api", "database.migrate", "检查并升级数据库", detailed=False):
            migrate(settings)
        from .settings import storage_summary
        storage = storage_summary(settings.database_url)
        logs.event("api", "database.ready", f"工作台数据库：SQLite 本地文件 {storage['path']}" if storage["backend"] == "sqlite" else "工作台数据库：外部数据库服务")
        logs.event("api", "auth.ready", f"本机访问令牌文件：{settings.data_dir / 'access-token'}")
        if args.init_only:
            # Keep the existing initialization command's stdout contract.
            print(f"工作台数据库：SQLite 本地文件 {storage['path']}" if storage["backend"] == "sqlite" else "工作台数据库：外部数据库服务", flush=True)
            print(f"本机访问令牌文件：{settings.data_dir / 'access-token'}", flush=True)
        if not args.init_only:
            import uvicorn
            from .api import create_app
            from .configured_agent import apply_preset
            with logs.operation("api", "api.routes", "初始化 API 路由"):
                application = create_app(settings)
            with logs.operation("api", "agent_cli.preset", "应用 Agent CLI 初始配置"):
                application.state.preset_warning = apply_preset(configuration, settings)
            if application.state.preset_warning:
                logs.event("api", "agent_cli.preset", "Agent CLI 预置未能应用，请查看启动状态中的提示", level=30)
            service = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=args.port if args.port is not None else configuration.port,
                timeout_graceful_shutdown=configuration.graceful_timeout_seconds, log_config=None, access_log=False))
            if stop_event is not None:
                def shutdown():
                    stop_event.wait()
                    service.should_exit = True
                threading.Thread(target=shutdown, daemon=True).start()
            with logs.operation("api", "api.serve", "HTTP API 服务", detailed=False):
                service.run()


if __name__ == "__main__":
    main()
