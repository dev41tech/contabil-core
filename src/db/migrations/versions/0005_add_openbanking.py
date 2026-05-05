"""Add conexoes_bancarias table for Open Banking.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status_conexao_enum = sa.Enum(
        "pendente", "ativa", "expirada", "erro", name="status_conexao_enum"
    )

    op.create_table(
        "conexoes_bancarias",
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
        sa.Column("provedor", sa.String(20), nullable=False, server_default="mock"),
        sa.Column("item_id", sa.String(100), nullable=False),
        sa.Column("account_id_externo", sa.String(100), nullable=True),
        sa.Column("instituicao_nome", sa.String(200), nullable=False),
        sa.Column("instituicao_codigo", sa.String(10), nullable=True),
        sa.Column("banco_sigla", sa.String(20), nullable=False),
        sa.Column("agencia_numero", sa.String(20), nullable=True),
        sa.Column("conta_numero", sa.String(30), nullable=True),
        sa.Column(
            "status",
            status_conexao_enum,
            nullable=False,
            server_default="pendente",
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "total_transacoes_sync",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column("erro_msg", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_conexao_empresa", "conexoes_bancarias", ["empresa_id"])
    op.create_index(
        "ix_conexao_status", "conexoes_bancarias", ["empresa_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_conexao_status", table_name="conexoes_bancarias")
    op.drop_index("ix_conexao_empresa", table_name="conexoes_bancarias")
    op.drop_table("conexoes_bancarias")
    op.execute("DROP TYPE IF EXISTS status_conexao_enum")
