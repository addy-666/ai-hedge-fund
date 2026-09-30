"""calibration models lifecycle

calibration_models gains a source (analyst / committee), a status (CANDIDATE / ACTIVE / REJECTED / RETIRED,
replacing the ``active`` flag), details and who decided when (roadmap 8.5: the operator approves a candidate,
or ``learning.calibration.activation: auto`` does). Nothing ever wrote the table before, so it is recreated.

Revision ID: d91f4b6a2e08
Revises: c3a8e1f25d70
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import aifund.persistence.types

# revision identifiers, used by Alembic.
revision: str = "d91f4b6a2e08"
down_revision: str | Sequence[str] | None = "c3a8e1f25d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("CANDIDATE", "ACTIVE", "REJECTED", "RETIRED")


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("calibration_models")
    op.create_table(
        "calibration_models",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                *STATUSES, name="calibrationstatus", native_enum=False, create_constraint=True, length=24
            ),
            nullable=False,
        ),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("n_samples", sa.Integer(), nullable=False),
        sa.Column("brier_before", sa.Float(), nullable=True),
        sa.Column("brier_after", sa.Float(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", aifund.persistence.types.UtcDateTime(length=27), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", aifund.persistence.types.UtcDateTime(length=27), nullable=True),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_calibration_models")),
    )
    op.create_index(
        "ix_calibration_models_source_status", "calibration_models", ["source", "status"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema (drops every fitted calibration model)."""
    op.drop_index("ix_calibration_models_source_status", table_name="calibration_models")
    op.drop_table("calibration_models")
    op.create_table(
        "calibration_models",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("n_samples", sa.Integer(), nullable=False),
        sa.Column("brier_before", sa.Float(), nullable=True),
        sa.Column("brier_after", sa.Float(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", aifund.persistence.types.UtcDateTime(length=27), nullable=False),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_calibration_models")),
    )
