"""Add preferences.llm_profile_id.

Additive, reversible migration: a nullable text column that stores the id of the
selected language-model profile (``work`` or ``ollama:<name>``). ``NULL`` means
the work profile. No existing row is changed. The downgrade drops only this
column, so only the profile choice is lost.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-11-07 01:00:00.000000+00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable ``preferences.llm_profile_id`` column."""
    op.add_column("preferences", sa.Column("llm_profile_id", sa.String(120), nullable=True))


def downgrade() -> None:
    """Drop ``preferences.llm_profile_id`` (batch mode covers SQLite)."""
    with op.batch_alter_table("preferences") as batch:
        batch.drop_column("llm_profile_id")
