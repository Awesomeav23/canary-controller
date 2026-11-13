from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from canary.models import Base


def make_sessionmaker(url: str) -> sessionmaker:
    if url.startswith("sqlite"):
        # In-memory SQLite for tests: one shared connection, usable across threads.
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    return sessionmaker(bind=engine)


def init_db(sessions: sessionmaker) -> None:
    # Fine for a scaffold; swap for Alembic migrations before the schema starts changing.
    Base.metadata.create_all(bind=sessions.kw["bind"])


def drop_db(sessions: sessionmaker) -> None:
    Base.metadata.drop_all(bind=sessions.kw["bind"])
