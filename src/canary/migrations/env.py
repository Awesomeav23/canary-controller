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


MIGRATION_LOCK = "canary_migrations"
LOCK_TIMEOUT_SECONDS = 300


def _run(connection: Connection) -> None:
    # SQLite can't ALTER most things in place; batch mode rebuilds the table instead.
    _configure(connection=connection, render_as_batch=connection.dialect.name == "sqlite")
    if connection.dialect.name != "mysql":
        with context.begin_transaction():
            context.run_migrations()
        return
    # MySQL DDL isn't transactional, so two runners at once (two pods starting together)
    # both apply the same ALTER and the second fails half-way. A named lock serializes
    # them: the second waits, then reads the new version and has nothing to do.
    got = connection.exec_driver_sql(
        f"SELECT GET_LOCK('{MIGRATION_LOCK}', {LOCK_TIMEOUT_SECONDS})"
    ).scalar()
    if got != 1:
        raise RuntimeError(f"another migration still holds {MIGRATION_LOCK!r}")
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.exec_driver_sql(f"SELECT RELEASE_LOCK('{MIGRATION_LOCK}')")


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
