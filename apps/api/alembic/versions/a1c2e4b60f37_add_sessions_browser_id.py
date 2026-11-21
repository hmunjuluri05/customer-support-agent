"""Add sessions.browser_id so a browser can resume its own earlier sessions.

Revision ID: a1c2e4b60f37
Revises: 5f6c69cee611
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c2e4b60f37"
down_revision: str | None = "5f6c69cee611"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: sessions created before this exist and are still valid, they
    # simply cannot be offered for resumption. Backfilling would mean inventing
    # an owner for them, which is the one thing this column must never do.
    op.add_column("sessions", sa.Column("browser_id", sa.String(length=64), nullable=True))
    op.create_index("ix_sessions_browser_id", "sessions", ["browser_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_browser_id", table_name="sessions")
    op.drop_column("sessions", "browser_id")
