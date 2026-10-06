"""intent setup tag

order_intents.setup_tag: the strategy an OPEN intent trades (roadmap 10.7). With family slots (10.8) an open
position's family is read from it, and the per-strategy daily cap (10.9) counts it. Intents written before
have none: their positions count as every family's (the conservative reading).

Revision ID: f2c8a5e1b7d4
Revises: e7b1c4d9a2f3
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c8a5e1b7d4"
down_revision: str | Sequence[str] | None = "e7b1c4d9a2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. A plain ADD COLUMN, not batch mode: batch mode rebuilds the table, and DROP TABLE
    order_intents fails while other tables reference it (foreign keys on; the restore drill caught it)."""
    op.add_column("order_intents", sa.Column("setup_tag", sa.String(length=40), nullable=True))


def downgrade() -> None:
    """Downgrade schema (SQLite >= 3.35 drops a column in place)."""
    op.drop_column("order_intents", "setup_tag")
