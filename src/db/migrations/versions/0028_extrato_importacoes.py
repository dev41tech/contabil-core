"""Cada upload de extrato vira um lote, e a transação sabe de qual veio.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-25

A transação não guardava de qual arquivo veio, e isso bloqueava três coisas ao
mesmo tempo: remover um extrato específico, desfazer um upload errado, e ordenar
com estabilidade lançamentos do mesmo dia vindos de arquivos diferentes —
`Transacao.ordem` é posição DENTRO de um arquivo, então duas importações
começam do zero e as posições colidem.

SEM BACKFILL, DE PROPÓSITO

As transações anteriores ficam com `importacao_id` nulo. Ao contrário da 0027,
onde `created_at` preservava a ordem do arquivo e permitia reconstruir, aqui não
há nada no banco que diga qual upload trouxe qual linha: agrupar por
`created_at` juntaria uploads distintos feitos no mesmo minuto e separaria um
upload que demorou. Inventar lote seria pior que não ter — a tela consegue dizer
"importação anterior ao registro de lotes", mas não consegue desfazer um lote
que nunca existiu.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extrato_importacoes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("empresa_id", sa.Uuid(), nullable=False),
        sa.Column("agencia_id", sa.Uuid(), nullable=False),
        sa.Column("nome_arquivo", sa.String(length=255), nullable=False),
        sa.Column("hash_arquivo", sa.String(length=64), nullable=False),
        sa.Column("total_no_arquivo", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("importadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejeitadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("criado_por", sa.Uuid(), nullable=True),
        sa.Column("cancelada_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelada_por", sa.Uuid(), nullable=True),
        sa.Column("motivo_cancelamento", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        sa.ForeignKeyConstraint(["agencia_id"], ["agencias_bancarias.id"]),
        sa.ForeignKeyConstraint(["criado_por"], ["usuarios.id"]),
        sa.ForeignKeyConstraint(["cancelada_por"], ["usuarios.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_importacao_empresa_criada",
        "extrato_importacoes",
        ["empresa_id", "created_at"],
    )
    op.add_column(
        "transacoes", sa.Column("importacao_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_transacao_importacao",
        "transacoes",
        "extrato_importacoes",
        ["importacao_id"],
        ["id"],
    )
    op.create_index("ix_transacao_importacao", "transacoes", ["importacao_id"])


def downgrade() -> None:
    op.drop_index("ix_transacao_importacao", table_name="transacoes")
    op.drop_constraint("fk_transacao_importacao", "transacoes", type_="foreignkey")
    op.drop_column("transacoes", "importacao_id")
    op.drop_index("ix_importacao_empresa_criada", table_name="extrato_importacoes")
    op.drop_table("extrato_importacoes")
