"""Add comprovantes table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comprovantes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id"),
            nullable=False,
        ),
        sa.Column(
            "agencia_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agencias_bancarias.id"),
            nullable=True,
        ),
        sa.Column(
            "transacao_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transacoes.id"),
            nullable=True,
        ),
        sa.Column("favorecido", sa.String(300), nullable=True),
        sa.Column("cpf_cnpj", sa.String(18), nullable=True),
        sa.Column("data_pagamento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_vencimento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valor_documento", sa.Numeric(15, 2), nullable=True),
        sa.Column("valor_pago", sa.Numeric(15, 2), nullable=False),
        sa.Column(
            "juros", sa.Numeric(15, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "multa", sa.Numeric(15, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "desconto", sa.Numeric(15, 2), nullable=False, server_default="0"
        ),
        sa.Column("observacao", sa.String(500), nullable=True),
        sa.Column("arquivo_nome", sa.String(255), nullable=True),
        sa.Column("arquivo_base64", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_comprovante_empresa", "comprovantes", ["empresa_id"])
    op.create_index(
        "ix_comprovante_transacao", "comprovantes", ["transacao_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_comprovante_transacao", table_name="comprovantes")
    op.drop_index("ix_comprovante_empresa", table_name="comprovantes")
    op.drop_table("comprovantes")
