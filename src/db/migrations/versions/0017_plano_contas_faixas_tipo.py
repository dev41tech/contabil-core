"""Adiciona faixas de código -> tipo contábil, configuráveis por empresa.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-11

Fallback pra importação do plano de contas quando a planilha não tem
coluna "Tipo": se o código do lançamento cai numa faixa configurada pela
empresa, o tipo é inferido dali em vez de virar erro. Configuração
explícita do usuário — não é heurística automática do sistema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plano_contas_faixas_tipo",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id"),
            nullable=False,
        ),
        sa.Column("tipo", sa.String(50), nullable=False),
        sa.Column("codigo_de", sa.String(30), nullable=False),
        sa.Column("codigo_ate", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_plano_contas_faixas_tipo_empresa",
        "plano_contas_faixas_tipo",
        ["empresa_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plano_contas_faixas_tipo_empresa", table_name="plano_contas_faixas_tipo"
    )
    op.drop_table("plano_contas_faixas_tipo")
