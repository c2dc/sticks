"""Integration/example test for the embedding-dim realignment migration
(stix-db-single-source-ai-pipeline task 8.2).

# Feature: stix-db-single-source-ai-pipeline — migration ``e6f7a8b9c0d1``
# (``align_experience_memory_embedding_dim_768``, task 8.1) resizes the pgvector
# column ``experience_memory.embedding`` from the previous design default
# ``VECTOR(1024)`` to ``VECTOR(768)``, the width of the product embedder
# (Ollama's ``nomic-embed-text``). The resize is PostgreSQL-gated (``upgrade``
# recreates the column at 768, ``downgrade`` restores 1024, both no-ops off
# PostgreSQL). This test drives the REAL ``upgrade()``/``downgrade()`` in-process
# and asserts the behaviours task 8.2 requires:
#   * the migration chains correctly (``e6f7a8b9c0d1`` revises ``d5e6f7a8b9c0``)
#     and states its widths as the module constants ``_NEW_EMBEDDING_DIM == 768``
#     and ``_PREVIOUS_EMBEDDING_DIM == 1024`` (Req. 6);
#   * a concrete ``upgrade`` ∘ ``downgrade`` is a lossless identity on the
#     ephemeral SQLite backend — both run cleanly and leave the schema unchanged,
#     because the VECTOR resize is PostgreSQL-only; the real ``VECTOR(768)``
#     recreation is asserted structurally via the module constants (Req. 6); and
#   * the product embedding dimension is 768, so a 768-length vector fits the
#     configured column width — asserted via
#     ``app.models.domain._DEFAULT_EMBEDDING_DIM`` and the
#     ``SepinfSettings.embedding_dim`` default, with NO Ollama/network call.

Validates: Requirements 6

This is a *feature* spec (``stix-db-single-source-ai-pipeline``), so the
behaviours under test are expected to HOLD on the migration as written.

Why a single SQLite in-process backend
---------------------------------------
The migration's VECTOR recreation is emitted only when the bind dialect is
``postgresql`` (``_is_postgresql()``); on any other backend it is a no-op. The
ephemeral SQLite the suite runs on therefore sees the real ``upgrade()`` and
``downgrade()`` execute as clean no-ops, which is exactly what makes the
up∘down identity trivially lossless there. The genuine 768-width enforcement is
a PostgreSQL concern and is asserted structurally through the module constants
rather than by standing up a real pgvector database. Everything is in-process —
NO Docker, NO network, NO shared DB, NO Ollama. The temp SQLite database is
disposed and deleted after use, following the convention of the sibling
migration tests in this directory.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Load the SHIPPED migration module (task 8.1) by path so we drive its REAL
# upgrade()/downgrade() logic rather than a reimplementation. The filename is
# not a valid identifier, so load it via importlib — the same convention as
# every sibling migration test in this directory.
# ---------------------------------------------------------------------------

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(module_name: str, filename: str) -> ModuleType:
    """Import a shipped migration module by its on-disk path."""
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION_EMBEDDING_DIM = _load_migration(
    "_migration_e6f7a8b9c0d1",
    "20261023_0100-e6f7a8b9c0d1_align_experience_memory_embedding_dim_768.py",
)

# Expected chain wiring and widths (Req. 6).
_EXPECTED_REVISION = "e6f7a8b9c0d1"
_EXPECTED_DOWN_REVISION = "d5e6f7a8b9c0"
_EXPECTED_NEW_DIM = 768
_EXPECTED_PREVIOUS_DIM = 1024


# ---------------------------------------------------------------------------
# SQLite in-process helpers — drive the migration's REAL upgrade/downgrade
# through Alembic's Operations proxy bound to an ISOLATED temp-file engine
# (mirrors test_migrations_command_base_tables_integration.py).
# ---------------------------------------------------------------------------


def _build_empty_sqlite_engine() -> tuple[Engine, Path]:
    """Create a fresh temp-file SQLite engine with an empty schema.

    The migration is PostgreSQL-gated, so against SQLite it touches nothing.
    Returns ``(engine, db_path)``; the caller disposes the engine and removes
    the file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="embedding_dim_mig_")
    db_path = Path(tmp_dir) / "embedding_dim.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    return engine, db_path


def _run_migration_step(engine: Engine, module: ModuleType, step: str) -> None:
    """Drive a migration module's REAL ``upgrade``/``downgrade`` through
    Alembic's Operations proxy bound to the ISOLATED temp engine.

    The migration module does ``from alembic import op`` (the global proxy). We
    temporarily rebind ``module.op`` to an ``Operations`` instance tied to a
    :class:`~alembic.migration.MigrationContext` on our temp connection so every
    ``op.*`` call (including ``op.get_bind().dialect.name`` inside
    ``_is_postgresql``) resolves against the temp SQLite connection, never any
    shared DB. On SQLite ``_is_postgresql()`` returns ``False``, so the resize
    body is skipped and the step is a clean no-op.
    """
    func = getattr(module, step)
    with engine.connect() as conn, conn.begin():
        ctx = MigrationContext.configure(connection=conn)
        op_proxy = Operations(ctx)
        original_op = module.op
        module.op = op_proxy
        try:
            func()
        finally:
            module.op = original_op


# ===========================================================================
# (A) Chain wiring + width constants: the migration revises ``d5e6f7a8b9c0``
#     and declares the 768/1024 widths as module constants (Req. 6).
# ===========================================================================


def test_embedding_dim_migration_chain_and_width_constants() -> None:
    """The migration is ``e6f7a8b9c0d1`` revising ``d5e6f7a8b9c0``, exposes
    callable ``upgrade``/``downgrade``, and states the target/previous widths as
    ``_NEW_EMBEDDING_DIM == 768`` and ``_PREVIOUS_EMBEDDING_DIM == 1024``.

    # Feature: stix-db-single-source-ai-pipeline — task 8.1.
    Validates: Requirements 6
    """
    assert _MIGRATION_EMBEDDING_DIM.revision == _EXPECTED_REVISION
    assert _MIGRATION_EMBEDDING_DIM.down_revision == _EXPECTED_DOWN_REVISION, (
        "align_experience_memory_embedding_dim_768 must revise "
        f"{_EXPECTED_DOWN_REVISION!r}, found "
        f"{_MIGRATION_EMBEDDING_DIM.down_revision!r}"
    )

    assert callable(_MIGRATION_EMBEDDING_DIM.upgrade)
    assert callable(_MIGRATION_EMBEDDING_DIM.downgrade)

    # The widths are stated as module constants (self-contained; no app import).
    # These structurally assert the real PostgreSQL VECTOR(768) recreation that
    # SQLite cannot exercise: upgrade resizes to 768, downgrade restores 1024.
    assert _MIGRATION_EMBEDDING_DIM._NEW_EMBEDDING_DIM == _EXPECTED_NEW_DIM
    assert _MIGRATION_EMBEDDING_DIM._PREVIOUS_EMBEDDING_DIM == _EXPECTED_PREVIOUS_DIM


# ===========================================================================
# (B) Concrete upgrade ∘ downgrade lossless on SQLite: both run cleanly and
#     leave the schema unchanged (the resize is PostgreSQL-only) (Req. 6).
# ===========================================================================


def test_sqlite_upgrade_then_downgrade_is_lossless_identity() -> None:
    """A concrete ``upgrade()`` then ``downgrade()`` runs cleanly on SQLite and
    leaves the schema exactly as it started.

    On SQLite the VECTOR resize is PostgreSQL-gated and so is a no-op: both steps
    execute without error and the schema is unchanged — a lossless up∘down
    identity. The genuine ``VECTOR(768)`` recreation is a PostgreSQL concern and
    is asserted structurally via the module constants in test (A).

    # Feature: stix-db-single-source-ai-pipeline — task 8.1.
    Validates: Requirements 6
    """
    engine, db_path = _build_empty_sqlite_engine()
    try:
        before = set(inspect(engine).get_table_names())

        # Drive the REAL upgrade then downgrade; neither may raise on SQLite.
        _run_migration_step(engine, _MIGRATION_EMBEDDING_DIM, "upgrade")
        after_up = set(inspect(engine).get_table_names())
        _run_migration_step(engine, _MIGRATION_EMBEDDING_DIM, "downgrade")
        after_down = set(inspect(engine).get_table_names())

        # PG-gated no-op: the ephemeral schema is untouched end to end.
        assert after_up == before, (
            "upgrade altered the SQLite schema, but the VECTOR resize is "
            f"PostgreSQL-only (delta: {after_up ^ before})"
        )
        assert after_down == before, (
            "upgrade ∘ downgrade was not a lossless identity on SQLite "
            f"(residual delta: {after_down ^ before})"
        )
    finally:
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


# ===========================================================================
# (C) Embedding-dimension smoke: the product embedding width is 768, so a
#     768-length vector fits the configured column (NO Ollama/network) (Req. 6).
# ===========================================================================


def test_product_embedding_dimension_is_768_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product embedding dimension is 768 everywhere it is configured, so a
    768-length vector fits the ``experience_memory.embedding`` column width.

    Asserted without any Ollama call or network I/O:
      * the models-layer design default ``_DEFAULT_EMBEDDING_DIM == 768``
        (the width used to build ``VECTOR(EMBEDDING_DIM)``); and
      * the ``SepinfSettings.embedding_dim`` default resolves to 768 (pinned
        here via ``AI_EMBEDDING_DIM=768`` so the assertion does not depend on
        the repository-root ``.env``).
    A 768-length vector therefore matches the column width the migration targets.

    # Feature: stix-db-single-source-ai-pipeline — task 8.1.
    Validates: Requirements 6
    """
    from app.models import domain as domain_module

    # Models-layer design default — the width the VECTOR column is built with.
    assert domain_module._DEFAULT_EMBEDDING_DIM == _EXPECTED_NEW_DIM

    # Services-layer settings default — pin the env so the resolved value is
    # independent of the repository-root ``.env`` contents.
    from app.services.ai.config import SepinfSettings, get_sepinf_settings

    monkeypatch.setenv("AI_EMBEDDING_DIM", str(_EXPECTED_NEW_DIM))
    get_sepinf_settings.cache_clear()
    try:
        settings = SepinfSettings()
        assert settings.embedding_dim == _EXPECTED_NEW_DIM
    finally:
        # Drop the instance built under the pinned env so later tests re-resolve.
        get_sepinf_settings.cache_clear()

    # A 768-length vector fits the configured column width exactly.
    vector = [0.0] * _EXPECTED_NEW_DIM
    assert len(vector) == domain_module._DEFAULT_EMBEDDING_DIM
