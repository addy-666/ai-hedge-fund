"""committee shadow arm

virtual_trades.arm gains SHADOW_COMMITTEE: what the Phase 8 committee (specialists + risk critic) would have
traded on a bar, recorded beside the analyst's and the baseline's shadows (roadmap 8.4). SQLite keeps the
allowed values in a CHECK constraint, so it is rebuilt.

Revision ID: c3a8e1f25d70
Revises: b56f9b78b406
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a8e1f25d70"
down_revision: str | Sequence[str] | None = "b56f9b78b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = ("BLOCKED", "SHADOW_BASELINE", "SHADOW_ANALYST")
AFTER = (*BEFORE, "SHADOW_COMMITTEE")


def _arm(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="virtualarm", native_enum=False, create_constraint=True, length=24)


def _rebuild(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    with op.batch_alter_table("virtual_trades", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_virtual_trades_virtualarm"), type_="check")
        batch_op.alter_column("arm", existing_type=_arm(old), type_=_arm(new), existing_nullable=False)


def upgrade() -> None:
    """Upgrade schema."""
    _rebuild(BEFORE, AFTER)


def downgrade() -> None:
    """Downgrade schema (fails if SHADOW_COMMITTEE rows exist: delete them first)."""
    _rebuild(AFTER, BEFORE)
