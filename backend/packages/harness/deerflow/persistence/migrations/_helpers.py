"""Idempotent helpers for alembic column revisions.

Column revisions in ``versions/`` should use these helpers instead of raw
``op.add_column`` / ``op.drop_column`` so re-running a column change against a
DB that already has (or has already removed) the column is a safe no-op.

Two reasons we need idempotency:

1. **Defence-in-depth on top of bootstrap locking.** ``bootstrap_schema()``
   serialises Postgres with an advisory lock and SQLite within one process
   with an ``asyncio.Lock``. If a retry happens anyway (manual ALTER,
   misconfiguration, SQLite cross-process contention), the revision must still
   be safe to re-run.

2. **Same posture that made ``Base.metadata.create_all`` forgiving.**
   ``create_all`` skips existing tables. Column migrations should mirror that
   forgiving behavior by skipping columns already in the desired state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def safe_add_column(table: str, column: sa.Column) -> None:
    """``op.add_column`` that no-ops when the table or column is missing/present.

    - Missing table => nothing to add to. Skip silently because bootstrap only
      supports legacy DBs that already have the baseline table set.
    - Column already exists => no-op.
    """
    insp = _inspector()
    if table not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(table)}
    if column.name in existing:
        return
    with op.batch_alter_table(table) as batch:
        batch.add_column(column)


def safe_drop_column(table: str, column_name: str) -> None:
    """``op.drop_column`` that no-ops when the table or column is already gone."""
    insp = _inspector()
    if table not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(table)}
    if column_name not in existing:
        return
    with op.batch_alter_table(table) as batch:
        batch.drop_column(column_name)
