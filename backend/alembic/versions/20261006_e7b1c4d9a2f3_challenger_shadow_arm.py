"""challenger shadow arm

virtual_trades.arm gains SHADOW_CHALLENGER: what the challenger analyst prompt (a second analyst, e.g.
analyst_v2 with the cross-asset context) would have traded on a bar, recorded beside the analyst's, the
baseline's and the committee's shadows — the prompt A/B of roadmap 10.4. SQLite keeps the allowed values in a
CHECK constraint, so it is rebuilt.

Revision ID: e7b1c4d9a2f3
Revises: d91f4b6a2e08
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b1c4d9a2f3"
down_revision: str | Sequence[str] | None = "d91f4b6a2e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = ("BLOCKED", "SHADOW_BASELINE", "SHADOW_ANALYST", "SHADOW_COMMITTEE")
AFTER = (*BEFORE, "SHADOW_CHALLENGER")


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
    """Downgrade schema (fails if SHADOW_CHALLENGER rows exist: delete them first)."""
    _rebuild(AFTER, BEFORE)
