"""virtual trade lifecycle

Virtual trades are recorded PENDING when a signal is blocked (entry, SL and TP unknown until the next
trigger bar opens), keep the planned stop/target distances and their expiry, have checked-enum status and
exit reason, and exist at most once per decision (roadmap 3.4). The table held no rows before this.

Revision ID: 718e999a1b7b
Revises: 7e2eb290adbf
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import aifund.persistence.types

# revision identifiers, used by Alembic.
revision: str = "718e999a1b7b"
down_revision: str | Sequence[str] | None = "7e2eb290adbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLOSE_REASONS = (
    "SL", "TP", "STOP_OUT", "ENGINE", "OPERATOR", "MANUAL_EXTERNAL", "REVERSAL", "TIME_STOP", "FLATTEN",
)  # fmt: skip
STATUSES = ("PENDING", "OPEN", "CLOSED", "EXPIRED", "NO_ENTRY")


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=24)


def upgrade() -> None:
    """Upgrade schema."""
    decimal = aifund.persistence.types.DecimalText(length=64)
    with op.batch_alter_table("virtual_trades", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sl_distance", decimal, nullable=False))
        batch_op.add_column(sa.Column("tp_distance", decimal, nullable=False))
        batch_op.add_column(
            sa.Column("expires_at", aifund.persistence.types.UtcDateTime(length=27), nullable=False)
        )
        batch_op.add_column(sa.Column("expire_reason", _enum(CLOSE_REASONS, "expire_reason"), nullable=False))
        for column in ("entry_price", "sl", "tp"):
            batch_op.alter_column(column, existing_type=sa.VARCHAR(length=64), nullable=True)
        batch_op.alter_column(
            "status", existing_type=sa.VARCHAR(length=16), type_=_enum(STATUSES, "virtualstatus"),
            existing_nullable=False,
        )  # fmt: skip
        batch_op.alter_column(
            "exit_reason", existing_type=sa.VARCHAR(length=24), type_=_enum(CLOSE_REASONS, "exit_reason"),
            existing_nullable=True,
        )  # fmt: skip
        batch_op.create_unique_constraint(batch_op.f("uq_virtual_trades_decision_id"), ["decision_id"])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("virtual_trades", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_virtual_trades_decision_id"), type_="unique")
        for ck in (
            "ck_virtual_trades_exit_reason",
            "ck_virtual_trades_virtualstatus",
            "ck_virtual_trades_expire_reason",
        ):
            batch_op.drop_constraint(op.f(ck), type_="check")
        batch_op.alter_column("exit_reason", type_=sa.String(length=24), existing_nullable=True)
        batch_op.alter_column("status", type_=sa.String(length=16), existing_nullable=False)
        for column in ("entry_price", "sl", "tp"):
            batch_op.alter_column(column, existing_type=sa.VARCHAR(length=64), nullable=False)
        batch_op.drop_column("expire_reason")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("tp_distance")
        batch_op.drop_column("sl_distance")
