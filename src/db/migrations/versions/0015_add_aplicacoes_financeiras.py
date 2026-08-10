"""Add aplicacoes_financeiras table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-10

Item 5 do levantamento de produto da SINCOPEÇAS de 10/08: "Falta um local
para registrar/tratar aplicações financeiras da empresa (não existe hoje
como conceito no sistema)". Tabela nova, sem relação com nenhuma existente
além de empresa (obrigatória) e agência bancária (opcional, quando a
aplicação está vinculada a uma conta específica).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "aplicacoes_financeiras",
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
        sa.Column("instituicao", sa.String(200), nullable=False),
        sa.Column("tipo", sa.String(30), nullable=False),
        sa.Column("descricao", sa.String(300), nullable=True),
        sa.Column("valor_aplicado", sa.Numeric(15, 2), nullable=False),
        sa.Column("data_aplicacao", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valor_atual", sa.Numeric(15, 2), nullable=True),
        sa.Column("data_atualizacao_valor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_vencimento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observacao", sa.String(500), nullable=True),
        sa.Column("ativa", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_aplicacao_financeira_empresa", "aplicacoes_financeiras", ["empresa_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_aplicacao_financeira_empresa", table_name="aplicacoes_financeiras"
    )
    op.drop_table("aplicacoes_financeiras")
