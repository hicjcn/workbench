from alembic import context
from sqlalchemy import create_engine
from agent_workbench.store import metadata

configuration = context.config
engine = create_engine(configuration.get_main_option("sqlalchemy.url"))
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()

