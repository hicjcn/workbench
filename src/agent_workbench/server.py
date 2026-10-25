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
    args = parser.parse_args()
    settings = Settings.load(args.data_dir)
    from .resources import controller_lock
    with controller_lock(settings.data_dir):
        migrate(settings)
        print(f"本机访问令牌文件：{settings.data_dir / 'access-token'}", flush=True)
        if not args.init_only:
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
