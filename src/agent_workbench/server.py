import argparse
from pathlib import Path

from .settings import Settings


def migrate(settings):
    from alembic.config import Config
    from alembic import command
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8340)
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--sqlite", action="store_true", help="使用数据目录中的本地 workbench.db，忽略 AWB_DATABASE_URL")
    args = parser.parse_args()
    settings = Settings.load(args.data_dir, sqlite=args.sqlite)
    from .resources import controller_lock
    with controller_lock(settings.data_dir):
        migrate(settings)
        from .settings import storage_summary
        storage = storage_summary(settings.database_url)
        print(f"工作台数据库：SQLite 本地文件 {storage['path']}" if storage["backend"] == "sqlite" else "工作台数据库：外部数据库服务", flush=True)
        print(f"本机访问令牌文件：{settings.data_dir / 'access-token'}", flush=True)
        if not args.init_only:
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
