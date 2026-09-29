"""shadow virtual trades and the SHADOW decision outcome

virtual_trades gains ``arm``: BLOCKED (a blocked signal, roadmap 3.4) or one of the two G-LLM shadows,
SHADOW_BASELINE and SHADOW_ANALYST (roadmap R.9), so one decision may carry up to three virtual trades,
one per arm (the unique key moves from decision_id to (decision_id, arm); existing rows are BLOCKED).
decisions.outcome gains SHADOW: the analyst decided in shadow and no baseline traded instead.

Revision ID: b7d2e4f9c1a3
Revises: 068109ac27bb
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d2e4f9c1a3"
down_revision: str | Sequence[str] | None = "068109ac27bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARMS = ("BLOCKED", "SHADOW_BASELINE", "SHADOW_ANALYST")
BEFORE = (
    "SKIPPED", "NO_SETUP", "HOLD", "INVALID", "RULE_BLOCKED", "BELOW_THRESHOLD", "RISK_REJECTED", "ORDERED",
    "DRY_RUN", "ERROR",
)  # fmt: skip
AFTER = (*BEFORE[:-1], "SHADOW", "ERROR")


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=24)


def _outcomes(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    with op.batch_alter_table("decisions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_decisions_decisionoutcome"), type_="check")
        batch_op.alter_column(
            "outcome", existing_type=_enum(old, "decisionoutcome"), type_=_enum(new, "decisionoutcome"),
            existing_nullable=False,
        )  # fmt: skip


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("virtual_trades", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("arm", _enum(ARMS, "virtualarm"), nullable=False, server_default="BLOCKED")
        )
        batch_op.drop_constraint(op.f("uq_virtual_trades_decision_id"), type_="unique")
        batch_op.create_unique_constraint(op.f("uq_virtual_trades_decision_id_arm"), ["decision_id", "arm"])
    with op.batch_alter_table("virtual_trades", schema=None) as batch_op:
        batch_op.alter_column("arm", server_default=None, existing_type=_enum(ARMS, "virtualarm"))
    _outcomes(BEFORE, AFTER)


def downgrade() -> None:
    """Downgrade schema (fails if shadow rows or SHADOW decisions exist: delete them first)."""
    _outcomes(AFTER, BEFORE)
    with op.batch_alter_table("virtual_trades", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("uq_virtual_trades_decision_id_arm"), type_="unique")
        batch_op.drop_constraint(op.f("ck_virtual_trades_virtualarm"), type_="check")
        batch_op.drop_column("arm")
        batch_op.create_unique_constraint(op.f("uq_virtual_trades_decision_id"), ["decision_id"])
