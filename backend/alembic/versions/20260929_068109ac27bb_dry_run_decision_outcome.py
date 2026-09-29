"""dry run decision outcome

decisions.outcome gains DRY_RUN: the Risk Manager approved an order, dry-run mode recorded it and sent
nothing (roadmap 4.6). SQLite keeps the allowed values in a CHECK constraint, so it is rebuilt.

Revision ID: 068109ac27bb
Revises: a1c0eb351778
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "068109ac27bb"
down_revision: str | Sequence[str] | None = "a1c0eb351778"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = (
    "SKIPPED", "NO_SETUP", "HOLD", "INVALID", "RULE_BLOCKED", "BELOW_THRESHOLD", "RISK_REJECTED", "ORDERED",
    "ERROR",
)  # fmt: skip
AFTER = (*BEFORE[:-1], "DRY_RUN", "ERROR")


def _outcome(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="decisionoutcome", native_enum=False, create_constraint=True, length=24)


def _rebuild(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    with op.batch_alter_table("decisions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_decisions_decisionoutcome"), type_="check")
        batch_op.alter_column(
            "outcome", existing_type=_outcome(old), type_=_outcome(new), existing_nullable=False
        )


def upgrade() -> None:
    """Upgrade schema."""
    _rebuild(BEFORE, AFTER)


def downgrade() -> None:
    """Downgrade schema (fails if DRY_RUN rows exist: delete or re-label them first)."""
    _rebuild(AFTER, BEFORE)
