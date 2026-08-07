"""Impede contas externas ativas duplicadas.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_conexao_empresa_provedor_conta",
        "conexoes_bancarias",
        ["empresa_id", "provedor", "account_id_externo"],
        unique=True,
        postgresql_where=sa.text(
            "deleted_at IS NULL AND account_id_externo IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_conexao_empresa_provedor_conta",
        table_name="conexoes_bancarias",
    )
