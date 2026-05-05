"""Add tipo_sa column to plano_contas.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plano_contas",
        sa.Column(
            "tipo_sa",
            sa.String(1),
            nullable=False,
            server_default="A",
        ),
    )


def downgrade() -> None:
    op.drop_column("plano_contas", "tipo_sa")
