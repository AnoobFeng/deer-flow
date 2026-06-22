"""Tests for ``deerflow.persistence.bootstrap.bootstrap_schema``.

Covers the three-branch decision table:

| DB state                              | Action                                  |
|---------------------------------------|-----------------------------------------|
| empty                                 | create_all + stamp head                 |
| legacy (DeerFlow tables, no alembic_version) | stamp baseline + upgrade head    |
| versioned                             | upgrade head                            |

Each test seeds a temp SQLite to the relevant pre-state, runs
``bootstrap_schema``, and asserts both the resulting schema and the
``alembic_version`` row.

The legacy branch is exercised twice — once with the new column missing
(branch 2) and once with it already present (branch 3 in the seed sense, but
the same code path) — to prove the idempotent revision helpers handle both
sub-cases without bootstrap needing to know about a specific column.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

# Pre-import models so Base.metadata is populated before bootstrap reads it.
import deerflow.persistence.models  # noqa: F401
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import (
    _decide_state,
    _get_head_revision,
    bootstrap_schema,
)

# Mark only async tests via the decorator below; module-level pytestmark would
# spuriously warn for the sync ``TestDecideState`` cases.
asyncio_test = pytest.mark.asyncio


HEAD = "0002_runs_token_usage"
BASELINE = "0001_baseline"


def _url(tmp_path: Path, name: str = "test.db") -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"


async def _table_names(engine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))


async def _runs_columns(engine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: {col["name"] for col in sa.inspect(c).get_columns("runs")})


async def _runs_column_meta(engine, column_name: str) -> dict:
    async with engine.connect() as conn:
        cols = await conn.run_sync(lambda c: sa.inspect(c).get_columns("runs"))
    for c in cols:
        if c["name"] == column_name:
            return c
    raise AssertionError(f"column {column_name!r} not found in runs")


async def _alembic_version(engine) -> str | None:
    async with engine.connect() as conn:
        row = await conn.execute(sa.text("SELECT version_num FROM alembic_version"))
        return row.scalar()


async def _seed_legacy_without_column(engine) -> None:
    """Build the pre-#3658 schema: create_all, then drop the new column."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        # SQLite supports DROP COLUMN from 3.35.0; the test runner pins recent
        # Python which bundles a 3.40+ sqlite, so this is safe.
        await conn.execute(sa.text("ALTER TABLE runs DROP COLUMN token_usage_by_model"))


async def _seed_legacy_with_column(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# Branch 1: empty DB
# ---------------------------------------------------------------------------


@asyncio_test
async def test_empty_branch_creates_all_and_stamps_head(tmp_path: Path) -> None:
    engine = create_async_engine(_url(tmp_path))
    try:
        await bootstrap_schema(engine, backend="sqlite")
        tables = await _table_names(engine)
        for required in {
            "runs",
            "threads_meta",
            "feedback",
            "users",
            "run_events",
            "channel_connections",
            "channel_credentials",
            "channel_conversations",
            "channel_oauth_states",
            "alembic_version",
        }:
            assert required in tables, f"missing table: {required}"
        assert "token_usage_by_model" in await _runs_columns(engine)
        assert await _alembic_version(engine) == HEAD
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Branch 2: legacy DB without token_usage_by_model
# ---------------------------------------------------------------------------


@asyncio_test
async def test_legacy_without_column_branch_upgrades(tmp_path: Path) -> None:
    engine = create_async_engine(_url(tmp_path))
    try:
        await _seed_legacy_without_column(engine)
        assert "token_usage_by_model" not in await _runs_columns(engine)
        assert "alembic_version" not in await _table_names(engine)

        await bootstrap_schema(engine, backend="sqlite")

        assert "token_usage_by_model" in await _runs_columns(engine)
        assert await _alembic_version(engine) == HEAD
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Branch 3: legacy DB that ALREADY has the column (post-#3658 create_all,
# or user-applied manual ALTER). The branch is the same as the
# legacy-without-column case -- bootstrap stamps baseline and tries to
# upgrade. The idempotent revision helper (``safe_add_column``) silently
# skips when the column is present, so the schema does not change.
# ---------------------------------------------------------------------------


@asyncio_test
async def test_legacy_with_column_branch_upgrade_is_idempotent(tmp_path: Path) -> None:
    engine = create_async_engine(_url(tmp_path))
    try:
        await _seed_legacy_with_column(engine)
        assert "token_usage_by_model" in await _runs_columns(engine)
        assert "alembic_version" not in await _table_names(engine)
        cols_before = await _runs_columns(engine)

        await bootstrap_schema(engine, backend="sqlite")

        cols_after = await _runs_columns(engine)
        assert cols_after == cols_before, "idempotent upgrade should not alter schema"
        assert await _alembic_version(engine) == HEAD
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Branch 4: versioned DB
# ---------------------------------------------------------------------------


@asyncio_test
async def test_versioned_branch_is_noop_at_head(tmp_path: Path) -> None:
    engine = create_async_engine(_url(tmp_path))
    try:
        # First bootstrap takes us through the empty branch.
        await bootstrap_schema(engine, backend="sqlite")
        cols_before = await _runs_columns(engine)
        # Second call hits the versioned branch.
        await bootstrap_schema(engine, backend="sqlite")
        cols_after = await _runs_columns(engine)
        assert cols_after == cols_before
        assert await _alembic_version(engine) == HEAD
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Schema-parity guard: legacy-upgraded DB must end up structurally identical
# to a fresh DB on the columns the migration touches. This is the property
# that catches drift between ``Base.metadata`` and ``0002``'s DDL -- exactly
# the failure mode of the original #3682 bug, just at a different layer.
# ---------------------------------------------------------------------------


@asyncio_test
async def test_token_usage_column_parity_between_fresh_and_upgraded(tmp_path: Path) -> None:
    fresh = create_async_engine(_url(tmp_path, "fresh.db"))
    upgraded = create_async_engine(_url(tmp_path, "upgraded.db"))
    try:
        # Fresh DB -> empty branch -> create_all
        await bootstrap_schema(fresh, backend="sqlite")
        fresh_col = await _runs_column_meta(fresh, "token_usage_by_model")

        # Legacy DB -> stamp baseline + 0002 upgrade
        await _seed_legacy_without_column(upgraded)
        await bootstrap_schema(upgraded, backend="sqlite")
        upgraded_col = await _runs_column_meta(upgraded, "token_usage_by_model")

        # Pin the contract: the column must have the same nullability after
        # either bootstrap path. If 0002 ever drifts from the model's
        # ``Mapped[dict]`` (i.e. ``nullable=False``), this fires.
        assert fresh_col["nullable"] == upgraded_col["nullable"], (
            f"nullability drift: fresh={fresh_col['nullable']} upgraded={upgraded_col['nullable']}"
        )
        # The model declares Mapped[dict] (non-optional) -> NOT NULL.
        assert fresh_col["nullable"] is False
        assert upgraded_col["nullable"] is False
    finally:
        await fresh.dispose()
        await upgraded.dispose()


# ---------------------------------------------------------------------------
# _decide_state unit tests (pure function, no DB needed)
# ---------------------------------------------------------------------------


class TestDecideState:
    def test_empty(self):
        assert (
            _decide_state({"has_alembic_version": False, "has_deerflow_tables": False})
            == "empty"
        )

    def test_empty_with_unrelated_tables(self):
        # LangGraph checkpointer tables present but DeerFlow has nothing yet.
        # ``has_deerflow_tables`` is derived from the metadata intersection in
        # production, so the only thing the decision function needs is the
        # bool itself.
        assert (
            _decide_state({"has_alembic_version": False, "has_deerflow_tables": False})
            == "empty"
        )

    def test_legacy(self):
        assert (
            _decide_state({"has_alembic_version": False, "has_deerflow_tables": True})
            == "legacy"
        )

    def test_versioned(self):
        assert (
            _decide_state({"has_alembic_version": True, "has_deerflow_tables": True})
            == "versioned"
        )

    def test_versioned_takes_precedence_over_empty(self):
        # Pathological: alembic_version row exists but no managed tables yet
        # (e.g. someone restored only the alembic_version table from backup).
        # We still go versioned -> upgrade head, which is the right thing:
        # alembic will run every revision from base.
        assert (
            _decide_state({"has_alembic_version": True, "has_deerflow_tables": False})
            == "versioned"
        )


# ---------------------------------------------------------------------------
# Sanity: head revision is the one this module expects
# ---------------------------------------------------------------------------


def test_head_revision_is_token_usage_revision() -> None:
    assert _get_head_revision() == HEAD


def test_baseline_revision_id_is_known() -> None:
    """Detect a baseline rename: the bootstrap code hardcodes ``0001_baseline``
    as the stamp target for the legacy branch, so a rename would silently
    break that branch unless caught here."""
    from pathlib import Path  # noqa: PLC0415

    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    migrations_dir = Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    script = ScriptDirectory.from_config(cfg)
    all_ids = {rev.revision for rev in script.walk_revisions()}
    assert BASELINE in all_ids, f"baseline revision id {BASELINE!r} not found in {all_ids}"
