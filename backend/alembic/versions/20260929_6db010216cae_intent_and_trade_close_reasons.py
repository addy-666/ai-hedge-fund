"""intent and trade close reasons

Closing intents record why they close (TIME_STOP, FLATTEN, REVERSAL, ...) so the reconciler can give an
engine-closed trade its reason, and trades.close_reason becomes a checked enum instead of free text.

Revision ID: 6db010216cae
Revises: 27e75f1ca40c
Create Date: 2026-09-29 00:12:40.425268

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6db010216cae"
down_revision: str | Sequence[str] | None = "27e75f1ca40c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLOSE_REASONS = (
    "SL", "TP", "STOP_OUT", "ENGINE", "OPERATOR", "MANUAL_EXTERNAL", "REVERSAL", "TIME_STOP", "FLATTEN",
)  # fmt: skip


def _close_reason() -> sa.Enum:
    return sa.Enum(*CLOSE_REASONS, name="closereason", native_enum=False, create_constraint=True, length=24)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("order_intents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("close_reason", _close_reason(), nullable=True))
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.alter_column(
            "close_reason", existing_type=sa.String(length=24), type_=_close_reason(), existing_nullable=True
        )


def downgrade() -> None:
    """Downgrade schema."""
    # SQLite rebuilds the table in batch mode: drop the CHECK first or it would reference a missing column
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_trades_closereason"), type_="check")
        batch_op.alter_column(
            "close_reason", existing_type=_close_reason(), type_=sa.String(length=24), existing_nullable=True
        )
    with op.batch_alter_table("order_intents", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_order_intents_closereason"), type_="check")
        batch_op.drop_column("close_reason")
