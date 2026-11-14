"""The migrations must build exactly the schema the models describe.

Runs on SQLite by default and on real MySQL in CI (CANARY_TEST_DATABASE_URL).
"""

import json
import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from canary.api import create_app
from canary.config import Settings
from canary.db import (
    SchemaOutOfDateError,
    current_revision,
    downgrade,
    drop_db,
    head_revision,
    make_sessionmaker,
    migrate,
)
from canary.models import Base


@pytest.fixture
def blank_db():
    sessions = make_sessionmaker(os.getenv("CANARY_TEST_DATABASE_URL") or "sqlite://")
    engine = sessions.kw["bind"]
    drop_db(engine)
    yield sessions
    drop_db(engine)
    engine.dispose()


def schema_diff(engine) -> list:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)


def test_migrations_match_models(blank_db):
    # Fails when someone changes a model and forgets the migration (or vice versa).
    engine = blank_db.kw["bind"]
    migrate(engine)
    assert current_revision(engine) == head_revision()
    assert schema_diff(engine) == []


def test_downgrade_to_nothing_and_back(blank_db):
    engine = blank_db.kw["bind"]
    migrate(engine)
    downgrade(engine, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    migrate(engine)
    assert schema_diff(engine) == []


def test_started_at_backfill(blank_db):
    """0003 fills started_at from each rollout's first 'step' event."""
    engine = blank_db.kw["bind"]
    migrate(engine, "0002")
    with engine.begin() as conn:
        insert = text(
            "INSERT INTO rollouts (id, app, image, policy, state, step_index, created_at,"
            " updated_at) VALUES (:id, :app, 'img', :policy, 'succeeded', 2, :t, :t)"
        )
        conn.execute(insert, {"id": 1, "app": "ran", "policy": json.dumps({}), "t": "2026-01-01"})
        conn.execute(insert, {"id": 2, "app": "never", "policy": json.dumps({}), "t": "2026-01-01"})
        conn.execute(
            text(
                "INSERT INTO rollout_events (rollout_id, at, kind, message) VALUES"
                " (1, '2026-01-01 10:00:00', 'created', 'x'),"
                " (1, '2026-01-01 10:00:05', 'step', 'step 0'),"
                " (1, '2026-01-01 10:01:05', 'step', 'step 1')"
            )
        )
    migrate(engine)
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT app, started_at FROM rollouts")).all())
    assert str(rows["ran"]).startswith("2026-01-01 10:00:05")
    assert rows["never"] is None


def test_app_refuses_to_start_on_old_schema(blank_db):
    migrate(blank_db.kw["bind"], "0002")
    app = create_app(Settings(), sessions=blank_db, run_loop=False)
    with pytest.raises(SchemaOutOfDateError, match="at 0002, code expects 0003"):
        with TestClient(app):
            pass


def test_app_starts_on_current_schema(blank_db):
    migrate(blank_db.kw["bind"])
    app = create_app(Settings(), sessions=blank_db, run_loop=False)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
