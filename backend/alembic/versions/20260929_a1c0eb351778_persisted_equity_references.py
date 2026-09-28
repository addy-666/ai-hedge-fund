"""persisted equity references

engine_state keeps the loss-limit references across restarts: which trading day / week the day-start and
week-start equity belong to, and the week-start equity (day_start_equity and peak_equity already existed).
Roadmap 3.5.

Revision ID: a1c0eb351778
Revises: 718e999a1b7b
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import aifund.persistence.types

# revision identifiers, used by Alembic.
revision: str = "a1c0eb351778"
down_revision: str | Sequence[str] | None = "718e999a1b7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    moment = aifund.persistence.types.UtcDateTime(length=27)
    with op.batch_alter_table("engine_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("day_start_at", moment, nullable=True))
        batch_op.add_column(
            sa.Column("week_start_equity", aifund.persistence.types.DecimalText(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("week_start_at", moment, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("engine_state", schema=None) as batch_op:
        batch_op.drop_column("week_start_at")
        batch_op.drop_column("week_start_equity")
        batch_op.drop_column("day_start_at")
