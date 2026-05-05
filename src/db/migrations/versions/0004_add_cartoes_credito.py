"""Add cartoes_credito, faturas_cartao, lancamentos_cartao tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── cartoes_credito
    op.create_table(
        "cartoes_credito",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id"),
            nullable=False,
        ),
        sa.Column("nome", sa.String(200), nullable=False),
        sa.Column("bandeira", sa.String(20), nullable=False),
        sa.Column("ultimos_digitos", sa.String(4), nullable=True),
        sa.Column("dia_fechamento", sa.Integer, nullable=False),
        sa.Column("dia_vencimento", sa.Integer, nullable=False),
        sa.Column("limite", sa.Numeric(15, 2), nullable=True),
        sa.Column("ativo", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cartao_empresa", "cartoes_credito", ["empresa_id"])

    # ── faturas_cartao
    status_fatura_enum = sa.Enum("aberta", "fechada", "paga", name="status_fatura_enum")

    op.create_table(
        "faturas_cartao",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id"),
            nullable=False,
        ),
        sa.Column(
            "cartao_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cartoes_credito.id"),
            nullable=False,
        ),
        sa.Column("competencia", sa.String(7), nullable=False),
        sa.Column("data_fechamento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_vencimento", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "valor_total", sa.Numeric(15, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "status", status_fatura_enum, nullable=False, server_default="aberta"
        ),
        sa.Column(
            "transacao_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transacoes.id"),
            nullable=True,
        ),
        sa.Column("observacao", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_fatura_cartao_competencia", "faturas_cartao", ["cartao_id", "competencia"]
    )
    op.create_index("ix_fatura_empresa", "faturas_cartao", ["empresa_id"])
    op.create_index("ix_fatura_status", "faturas_cartao", ["empresa_id", "status"])

    # ── lancamentos_cartao
    op.create_table(
        "lancamentos_cartao",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id"),
            nullable=False,
        ),
        sa.Column(
            "fatura_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("faturas_cartao.id"),
            nullable=False,
        ),
        sa.Column("data_compra", sa.DateTime(timezone=True), nullable=False),
        sa.Column("descricao", sa.String(500), nullable=False),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column(
            "conta_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plano_contas.id"),
            nullable=True,
        ),
        sa.Column("parcela_atual", sa.Integer, nullable=True),
        sa.Column("parcela_total", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_lancamento_fatura", "lancamentos_cartao", ["fatura_id"])


def downgrade() -> None:
    op.drop_index("ix_lancamento_fatura", table_name="lancamentos_cartao")
    op.drop_table("lancamentos_cartao")

    op.drop_index("ix_fatura_status", table_name="faturas_cartao")
    op.drop_index("ix_fatura_empresa", table_name="faturas_cartao")
    op.drop_constraint("uq_fatura_cartao_competencia", "faturas_cartao")
    op.drop_table("faturas_cartao")
    op.execute("DROP TYPE IF EXISTS status_fatura_enum")

    op.drop_index("ix_cartao_empresa", table_name="cartoes_credito")
    op.drop_table("cartoes_credito")
