from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from canary.config import Settings
from canary.models import Base

config = context.config
target_metadata = Base.metadata

# Tests and the app pass an open connection in; only the CLI configures logging.
if config.config_file_name and "connection" not in config.attributes:
    fileConfig(config.config_file_name)


def _configure(**kwargs) -> None:
    context.configure(target_metadata=target_metadata, compare_type=True, **kwargs)


def _run(connection: Connection) -> None:
    # SQLite can't ALTER most things in place; batch mode rebuilds the table instead.
    _configure(connection=connection, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _configure(url=Settings.from_env().database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif (conn := config.attributes.get("connection")) is not None:
    _run(conn)
else:
    engine = create_engine(Settings.from_env().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)
