"""Add conta_numero to plano_contas (MrContador numeric ID + allow codigo edit).

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plano_contas",
        sa.Column("conta_numero", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plano_contas", "conta_numero")
