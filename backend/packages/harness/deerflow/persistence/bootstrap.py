"""Hybrid schema bootstrap for DeerFlow's application tables.

Replaces the unconditional ``Base.metadata.create_all`` at Gateway startup.
Combines two ideas:

1. ``create_all`` stays the empty-DB fast path -- it renders ``Base.metadata``
   faithfully across SQLite and Postgres dialects (JSON vs JSONB, server
   defaults, index/FK names, type affinity) without anyone having to hand-keep
   a mirror baseline in sync with the models.
2. **Alembic owns every change from baseline onward.** Any new ORM column /
   table / index must ship as a revision under ``migrations/versions/``.

Three-branch decision (see ``_decide_state``)
---------------------------------------------

| DB state                              | Action                                  |
|---------------------------------------|-----------------------------------------|
| empty (no DeerFlow tables)            | ``create_all`` + ``alembic stamp head`` |
| legacy (DeerFlow tables, no alembic)  | ``stamp 0001_baseline`` + ``upgrade head`` |
| versioned (``alembic_version`` row)   | ``alembic upgrade head``                |

The legacy branch handles pre-alembic databases that already have the baseline
application tables -- whether they were created before PR #3658 (no
``token_usage_by_model`` column), after #3658 via ``create_all`` (column
already there), or by a user who ran the manual ``ALTER`` from the issue. It
does not repair very old or hand-edited DBs that are missing tables from the
baseline itself. Distinguishing post-baseline sub-cases at the bootstrap layer
would force bootstrap to know about every column ever added, which is the
design smell this module avoids. Instead, each ``versions/*.py`` revision uses
the idempotent helpers in ``migrations/_helpers.py`` for column changes so
re-applying a revision against a DB that already has the change is a silent
no-op. Future schema additions therefore plug in by writing a new revision
file -- **no edit to this module is required**.

Concurrency safety
------------------

Layered, with different guarantees per backend. Postgres has true
cross-process serialisation. SQLite is single-process safe and cross-process
best-effort; multi-instance deployments should use Postgres.

* **Postgres -- true cross-process serialisation.** ``pg_advisory_lock`` runs
  the whole reflect-and-act sequence under an exclusive lock that survives
  cross-process. Concurrent Gateway instances queue cleanly and the second
  one observes head as a no-op.

* **SQLite -- single-process serialisation, best-effort cross-process.**
  SQLite is single-node by deployment, so the realistic concurrency case is
  multiple async tasks inside one Gateway process (tests, lifespan re-entry).
  A per-engine ``asyncio.Lock`` serialises those. For the rare cross-process
  case (e.g. two ``make dev`` workers on the same DB file), we rely on
  SQLite's own file-level write lock plus a 30s ``PRAGMA busy_timeout`` --
  the latter is set on **both** the production engine
  (``persistence/engine.py``) and the alembic-spawned engine
  (``migrations/env.py``) so any writer waits up to 30s for the file lock
  instead of failing fast. This is best-effort, not a true mutex: under
  pathological overlap a process can still see ``database is locked`` after
  30s. The fallback line of defence -- idempotent revisions -- guarantees
  correctness anyway.

* **Idempotent revisions -- retry fallback.** Column revisions use the helpers
  in ``migrations/_helpers.py`` so repeated post-baseline changes, manual
  ALTERs, or retries after SQLite lock contention do not duplicate work.

``alembic upgrade head`` on a DB already at head is a no-op by alembic's own
semantics, so the second-N-th actor simply observes head and exits.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


# Where the alembic environment lives, relative to this file.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Cached migration head, computed once per process from the disk script tree.
_HEAD_REVISION: str | None = None

# Baseline (stamp target for legacy DBs). Pinned here so the bootstrap layer
# fails loudly if the baseline revision is ever renamed without updating the
# stamp call. ``tests/test_persistence_bootstrap.py`` asserts this string is a
# real revision id in the script tree.
_BASELINE_REVISION = "0001_baseline"

# Stable advisory-lock key for Postgres. Two random 32-bit halves picked once
# so we never collide with any other application's advisory locks. Do not
# change without coordinating a one-time migration (a key change effectively
# releases the prior lock).
_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682


# Per-engine SQLite bootstrap locks. Per-engine (not module-global) so each
# engine instance pairs with a lock bound to the event loop that uses that
# engine -- necessary because ``asyncio.Lock`` binds to the first loop it sees,
# and pytest gives each async test its own loop. Production uses one engine
# per process so this dict collapses to a single entry in practice.
_SQLITE_LOCKS: dict[int, asyncio.Lock] = {}


def _get_sqlite_local_lock(engine: AsyncEngine) -> asyncio.Lock:
    key = id(engine)
    lock = _SQLITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SQLITE_LOCKS[key] = lock
    return lock


def _alembic_safe_url(engine: AsyncEngine) -> str:
    """Render *engine*'s URL in a form alembic ``set_main_option`` accepts.

    Two pitfalls handled:

    1. ``str(engine.url)`` (and ``URL.render_as_string()`` without args) masks
       the password as ``***`` -- so alembic's stamp/upgrade would open its own
       connection with garbage credentials and fail at runtime, even though
       the live engine connects fine. Fix: ``render_as_string(hide_password=False)``.
    2. ``alembic.config.Config.set_main_option`` forwards to
       ``ConfigParser.set``, which performs ``%(name)s``-style interpolation
       on the value. A URL-encoded password like ``p%40ss`` (``@`` escaped to
       ``%40``) would raise ``InterpolationSyntaxError``. Fix: double every
       literal ``%`` so ConfigParser unescapes it back to one.
    """
    rendered = engine.url.render_as_string(hide_password=False)
    return rendered.replace("%", "%%")


def _get_alembic_config(engine: AsyncEngine) -> AlembicConfig:
    """Build an in-process alembic config pointing at our migrations dir.

    Avoids reading ``alembic.ini`` from disk so the production runtime doesn't
    depend on a working-directory-relative file lookup. The ``script_location``
    is anchored at the package path on disk.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", _alembic_safe_url(engine))
    return cfg


def _get_head_revision() -> str:
    """Return the head revision id from ``versions/``, cached per process."""
    global _HEAD_REVISION
    if _HEAD_REVISION is None:
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        if head is None:
            raise RuntimeError("alembic has no head revision -- versions/ directory is empty")
        _HEAD_REVISION = head
    return _HEAD_REVISION


def _reflect_state(sync_conn: Any) -> dict[str, bool]:
    """Inspect *sync_conn* (sync connection inside ``run_sync``) and return:

    - ``has_alembic_version``: bool
    - ``has_deerflow_tables``: True iff at least one table that ``Base.metadata``
      knows about is present in the DB. Computed as ``reflected ∩ metadata`` so
      the bootstrap layer never hardcodes a specific table or column name --
      adding a new ORM model only changes ``Base.metadata``, not this module.
    """
    from deerflow.persistence.base import Base

    # Make sure every ORM model is imported, otherwise ``Base.metadata.tables``
    # may miss tables registered by submodules that haven't been imported yet.
    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; metadata may be incomplete")

    insp = sa_inspect(sync_conn)
    reflected = set(insp.get_table_names())
    metadata_tables = set(Base.metadata.tables)
    return {
        "has_alembic_version": "alembic_version" in reflected,
        "has_deerflow_tables": bool(reflected & metadata_tables),
    }


def _decide_state(state: dict[str, bool]) -> str:
    """Map a reflected DB state to one of three branch labels.

    The legacy branch covers every pre-alembic DB uniformly -- whether the
    columns added by later revisions are present or not is a question each
    revision answers for itself via the idempotent helpers in
    ``migrations/_helpers.py``.
    """
    if state["has_alembic_version"]:
        return "versioned"
    if not state["has_deerflow_tables"]:
        # Either a brand-new DB or a DB containing only tables we don't own
        # (e.g. LangGraph's checkpointer tables on a fresh deployment). The
        # empty branch provisions the tables alembic owns, then stamps head.
        return "empty"
    return "legacy"


def _run_create_all_sync(sync_conn: Any) -> None:
    """Create all DeerFlow-owned tables on *sync_conn*."""
    # Import here to ensure all model classes are registered with Base.metadata.
    from deerflow.persistence.base import Base

    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; bootstrap will create empty schema")

    Base.metadata.create_all(sync_conn)


def _stamp(cfg: AlembicConfig, revision: str) -> None:
    """Synchronous alembic stamp; callers must wrap in ``asyncio.to_thread``."""
    alembic_command.stamp(cfg, revision)


def _upgrade(cfg: AlembicConfig, revision: str) -> None:
    """Synchronous alembic upgrade; callers must wrap in ``asyncio.to_thread``."""
    alembic_command.upgrade(cfg, revision)


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _postgres_lock(engine: AsyncEngine):
    """Hold a Postgres session-level advisory lock for the body of the block.

    Session-level (not transaction-level) so the lock outlives implicit
    transactions opened by alembic during ``stamp`` / ``upgrade``. The lock
    is released explicitly on the way out and -- as a safety net -- when the
    backing session disconnects (process crash, kill -9).
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _PG_LOCK_KEY})
        try:
            logger.info("bootstrap: acquired postgres advisory lock key=0x%x", _PG_LOCK_KEY)
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _PG_LOCK_KEY})
            except Exception:  # noqa: BLE001
                logger.warning("bootstrap: pg_advisory_unlock raised; session close will release", exc_info=True)


@asynccontextmanager
async def _sqlite_lock(engine: AsyncEngine):
    """Serialise SQLite bootstrap inside one process; cross-process is
    best-effort via SQLite's own file lock + ``PRAGMA busy_timeout``.

    Why not ``BEGIN IMMEDIATE`` on a sentinel connection? SQLite is
    single-writer per file. If we held a write lock on one connection,
    alembic's own connection (opened inside ``stamp`` / ``upgrade``) would
    deadlock against us.

    Why not a cross-process OS file lock? It would work, but it adds a hard
    dependency on platform-specific ``fcntl`` / ``msvcrt`` calls for a
    deployment shape (multi-process SQLite) that's already discouraged for
    DeerFlow. The 30s ``busy_timeout`` plus idempotent revisions cover the
    realistic case; truly multi-instance deployments should use Postgres.

    Note: the 30s ``busy_timeout`` is set by the engine event hooks in
    ``persistence/engine.py`` (production) and ``migrations/env.py``
    (alembic-spawned). This function relies on those PRAGMAs being in place
    rather than setting one on a probe connection that wouldn't propagate.
    """
    async with _get_sqlite_local_lock(engine):
        logger.info("bootstrap: acquired sqlite in-process lock")
        yield


def _bootstrap_lock(engine: AsyncEngine, *, backend: str):
    if backend == "postgres":
        return _postgres_lock(engine)
    if backend == "sqlite":
        return _sqlite_lock(engine)
    raise ValueError(f"bootstrap: unsupported backend {backend!r}")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def bootstrap_schema(engine: AsyncEngine, *, backend: str) -> None:
    """Bring the DB schema to head.

    Postgres calls are serialised across processes with an advisory lock.
    SQLite calls are serialised inside one process and are best-effort across
    processes via SQLite's file lock and ``busy_timeout``.

    Branch dispatch is documented at module top. ``alembic.command.stamp`` and
    ``alembic.command.upgrade`` are synchronous and would block the event
    loop; both are wrapped in ``asyncio.to_thread``.
    """
    head = _get_head_revision()
    cfg = _get_alembic_config(engine)

    async with _bootstrap_lock(engine, backend=backend):
        async with engine.connect() as conn:
            state = await conn.run_sync(_reflect_state)
        decision = _decide_state(state)

        if decision == "empty":
            logger.info("bootstrap: branch=empty -> create_all + stamp head (%s)", head)
            async with engine.begin() as conn:
                await conn.run_sync(_run_create_all_sync)
            await asyncio.to_thread(_stamp, cfg, head)

        elif decision == "legacy":
            logger.info(
                "bootstrap: branch=legacy -> stamp %s + upgrade head (%s)",
                _BASELINE_REVISION,
                head,
            )
            await asyncio.to_thread(_stamp, cfg, _BASELINE_REVISION)
            await asyncio.to_thread(_upgrade, cfg, "head")

        elif decision == "versioned":
            logger.info("bootstrap: branch=versioned -> upgrade head (%s)", head)
            await asyncio.to_thread(_upgrade, cfg, "head")

        else:  # pragma: no cover -- defensive
            raise RuntimeError(f"bootstrap: unhandled decision {decision!r}")

    logger.info("bootstrap: complete (backend=%s)", backend)
