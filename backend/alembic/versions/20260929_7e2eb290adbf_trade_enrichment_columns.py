"""trade enrichment columns

MAE/MFE in price (the R values already existed), entry and exit slippage, and enriched_at: the marker that
a closed trade has been enriched (roadmap 3.3).

Revision ID: 7e2eb290adbf
Revises: 6db010216cae
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import aifund.persistence.types

# revision identifiers, used by Alembic.
revision: str = "7e2eb290adbf"
down_revision: str | Sequence[str] | None = "6db010216cae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("mae_price", aifund.persistence.types.DecimalText(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("mfe_price", aifund.persistence.types.DecimalText(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("entry_slippage_points", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("exit_slippage_points", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("enriched_at", aifund.persistence.types.UtcDateTime(length=27), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.drop_column("enriched_at")
        batch_op.drop_column("exit_slippage_points")
        batch_op.drop_column("entry_slippage_points")
        batch_op.drop_column("mfe_price")
        batch_op.drop_column("mae_price")
