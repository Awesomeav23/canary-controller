from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from canary.config import REPO_ROOT
from canary.models import Base


class SchemaOutOfDateError(RuntimeError):
    pass


def make_sessionmaker(url: str) -> sessionmaker:
    if url.startswith("sqlite"):
        # In-memory SQLite for tests: one shared connection, usable across threads.
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    return sessionmaker(bind=engine)


# --- migrations -------------------------------------------------------------


def alembic_config() -> Config:
    # alembic.ini only exists in a checkout; an installed package (the container image)
    # still finds its migrations by package name.
    ini = REPO_ROOT / "alembic.ini"
    cfg = Config(str(ini)) if ini.exists() else Config()
    cfg.set_main_option("script_location", "canary:migrations")
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def require_schema_current(engine: Engine) -> None:
    """Refuse to run against a database the code doesn't match.

    The app never migrates on startup: several replicas starting at once would race to
    apply the same migration. Migrations run as their own step (`make migrate`).
    """
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise SchemaOutOfDateError(
            f"database schema is at {current or 'nothing'}, code expects {head}: run `make migrate`"
        )


def migrate(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


# --- test helpers -----------------------------------------------------------


def init_db(sessions: sessionmaker) -> None:
    """Create tables straight from the models (fast, for tests) and mark them current.

    tests/test_migrations.py checks separately that the migrations build the same schema.
    """
    engine = sessions.kw["bind"]
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.stamp(cfg, "head")


def drop_db(sessions_or_engine: sessionmaker | Engine) -> None:
    engine = (
        sessions_or_engine.kw["bind"]
        if isinstance(sessions_or_engine, sessionmaker)
        else sessions_or_engine
    )
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
