"""align experience_memory.embedding to VECTOR(768) (stix-db-single-source-ai-pipeline task 8.1)

Chained onto ``d5e6f7a8b9c0``
(``create_stix_package_persistence_tables``), the current head. This is the
first sub-step of Phase 3 of the ``stix-db-single-source-ai-pipeline`` spec: it
aligns the pgvector width of ``experience_memory.embedding`` to the dimension of
the product embedder, Ollama's ``nomic-embed-text`` (768-dim, verified live),
down from the previous design default of 1024.

What this migration does (Req. 6)
---------------------------------
* **Recreates the embedding column at VECTOR(768) (PostgreSQL only).** pgvector
  requires a fixed vector width, so the width cannot be changed in place with a
  portable ``ALTER``; instead the column is dropped and re-added as
  ``VECTOR(768)``. The ``experience_memory`` table is empty (no persisted
  embeddings yet), so dropping and re-adding the column loses no data — the
  change is lossless in practice.
* **Rebuilds the cosine index that depends on the column.** The IVFFlat
  ``vector_cosine_ops`` index ``ix_experience_memory_embedding_cosine`` created
  by migration ``a1b2c3d4e5f6`` references ``embedding``; dropping the column
  drops the index, so ``upgrade`` recreates the index after re-adding the column
  (and ``downgrade`` does the same when restoring the previous width).
* **PostgreSQL only.** Like the pgvector-enabling migration ``a1b2c3d4e5f6``,
  the ``VECTOR`` column recreation and the IVFFlat index are emitted only when
  the bind dialect is ``postgresql``. On any other backend (the ephemeral SQLite
  the integration tests use) the portable ``pgvector.sqlalchemy.Vector`` type
  compiles to a plain column, so recreating it would be a no-op churn with no
  real width enforcement; the migration therefore skips entirely off
  PostgreSQL, leaving the SQLite schema untouched.

Additive / reversible (Req. 6)
------------------------------
The migration only resizes one column (and rebuilds the one index that depends
on it). It alters no other table, enum, CHECK or constraint and moves no data.
``downgrade`` restores the previous width (``VECTOR(1024)``) and rebuilds the
same cosine index, so ``upgrade`` ∘ ``downgrade`` is a lossless identity on the
schema (the empty table carries no rows to preserve either way).

Self-contained (precedent of every prior migration in this tree)
----------------------------------------------------------------
No application code is imported; the fixed widths are stated as module
constants. The dialect guard mirrors ``a1b2c3d4e5f6`` exactly.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-23 01:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The product embedder (``nomic-embed-text``) emits 768-dim vectors; this is the
# target width. ``_PREVIOUS_EMBEDDING_DIM`` is the width created by migration
# ``a1b2c3d4e5f6`` (the prior design default) that ``downgrade`` restores.
_NEW_EMBEDDING_DIM = 768
_PREVIOUS_EMBEDDING_DIM = 1024

# Name of the cosine-similarity index on ``experience_memory.embedding`` — the
# exact name created by migration ``a1b2c3d4e5f6``; rebuilt here after the
# column is resized (dropping the column drops its dependent index).
_EMBEDDING_INDEX_NAME = "ix_experience_memory_embedding_cosine"


def _is_postgresql() -> bool:
    """True when the live bind is PostgreSQL (where pgvector is available)."""
    return op.get_bind().dialect.name == "postgresql"


def _resize_embedding(target_dim: int) -> None:
    """Recreate ``experience_memory.embedding`` as ``VECTOR(target_dim)`` on PostgreSQL.

    pgvector requires a fixed width, so the column is dropped and re-added at the
    target dimension (the table is empty, so this is lossless). The cosine
    IVFFlat index that depends on the column is rebuilt afterwards. No-op off
    PostgreSQL, where the portable ``Vector`` type is a plain column.
    """
    if not _is_postgresql():
        return

    # Drop the dependent cosine index first (dropping the column would drop it
    # anyway; doing it explicitly keeps the intent clear and the step reversible).
    op.execute(f"DROP INDEX IF EXISTS {_EMBEDDING_INDEX_NAME}")

    # Recreate the column at the target width. The table is empty, so dropping
    # and re-adding the NOT NULL column loses no data.
    op.drop_column("experience_memory", "embedding")
    op.add_column(
        "experience_memory",
        sa.Column("embedding", Vector(target_dim), nullable=False),
    )

    # Rebuild the IVFFlat cosine index on the resized column (Req. 6).
    op.execute(
        f"CREATE INDEX {_EMBEDDING_INDEX_NAME} "
        "ON experience_memory USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def upgrade() -> None:
    _resize_embedding(_NEW_EMBEDDING_DIM)


def downgrade() -> None:
    _resize_embedding(_PREVIOUS_EMBEDDING_DIM)
