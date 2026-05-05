"""Add CONCILPRO tables (cp_arquivo, cp_fornecedor, cp_lancamento, cp_conciliacao, cp_divergencia).

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── cp_arquivo ──────────────────────────────────────────────────────────────
    op.create_table(
        "cp_arquivo",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nome_arquivo", sa.String(255), nullable=False),
        sa.Column("hash_arquivo", sa.String(64), nullable=False, unique=True),
        sa.Column("empresa", sa.String(255), nullable=True),
        sa.Column("cnpj_empresa", sa.String(18), nullable=True),
        sa.Column("total_fornecedores", sa.Integer(), nullable=True, default=0),
        sa.Column("total_lancamentos", sa.Integer(), nullable=True, default=0),
        sa.Column("data_inicio", sa.Date(), nullable=True),
        sa.Column("data_fim", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=True, default="PROCESSANDO"),
        sa.Column("mensagem_erro", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    # ── cp_fornecedor ───────────────────────────────────────────────────────────
    op.create_table(
        "cp_fornecedor",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("arquivo_origem_id", sa.Integer(), sa.ForeignKey("cp_arquivo.id"), nullable=True),
        sa.Column("codigo_conta", sa.String(10), nullable=False),
        sa.Column("conta_contabil", sa.String(50), nullable=False),
        sa.Column("nome_fornecedor", sa.Text(), nullable=False),
        sa.Column("cnpj", sa.String(18), nullable=True),
        sa.Column("saldo_anterior", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("saldo_anterior_tipo", sa.String(1), nullable=True),
        sa.Column("total_debito", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("total_credito", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("saldo_final", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("saldo_final_tipo", sa.String(1), nullable=True),
        sa.Column("status_pagamento", sa.String(20), nullable=True),
        sa.Column("valor_a_pagar", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("qtd_nfs_pendentes", sa.Integer(), nullable=True, default=0),
        sa.Column("qtd_nfs_parciais", sa.Integer(), nullable=True, default=0),
        sa.Column("divergencia_calculo", sa.Boolean(), nullable=True, default=False),
        sa.Column("mensagem_erro", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cp_fornecedor_conta", "cp_fornecedor", ["codigo_conta", "conta_contabil"])
    op.create_index("ix_cp_fornecedor_status", "cp_fornecedor", ["status_pagamento"])

    # ── cp_lancamento ───────────────────────────────────────────────────────────
    op.create_table(
        "cp_lancamento",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fornecedor_id", sa.Integer(), sa.ForeignKey("cp_fornecedor.id"), nullable=False),
        sa.Column("data_lancamento", sa.Date(), nullable=False),
        sa.Column("lote", sa.String(50), nullable=True),
        sa.Column("historico", sa.Text(), nullable=False),
        sa.Column("conta_partida", sa.String(20), nullable=True),
        sa.Column("valor_debito", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("valor_credito", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("saldo_apos_lancamento", sa.Numeric(15, 2), nullable=True),
        sa.Column("saldo_tipo", sa.String(1), nullable=True),
        sa.Column("tipo_operacao", sa.String(20), nullable=True),
        sa.Column("numero_nf", sa.String(50), nullable=True),
        sa.Column("cnpj_historico", sa.String(18), nullable=True),
        sa.Column("valor_pago_parcial", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("valor_saldo", sa.Numeric(15, 2), nullable=True, default=0),
        sa.Column("status_pagamento", sa.String(20), nullable=True),
        sa.Column("classificado_por_ia", sa.Boolean(), nullable=True, default=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cp_lancamento_forn_data", "cp_lancamento", ["fornecedor_id", "data_lancamento"])
    op.create_index("ix_cp_lancamento_tipo", "cp_lancamento", ["tipo_operacao"])
    op.create_index("ix_cp_lancamento_status", "cp_lancamento", ["status_pagamento"])
    op.create_index("ix_cp_lancamento_nf", "cp_lancamento", ["numero_nf"])

    # ── cp_conciliacao ──────────────────────────────────────────────────────────
    op.create_table(
        "cp_conciliacao",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fornecedor_id", sa.Integer(), sa.ForeignKey("cp_fornecedor.id"), nullable=False),
        sa.Column("lancamento_credito_id", sa.Integer(), sa.ForeignKey("cp_lancamento.id"), nullable=True),
        sa.Column("lancamento_debito_id", sa.Integer(), sa.ForeignKey("cp_lancamento.id"), nullable=True),
        sa.Column("valor_conciliado", sa.Numeric(15, 2), nullable=False),
        sa.Column("metodo_match", sa.String(20), nullable=True),
        sa.Column("confianca", sa.Integer(), nullable=True),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cp_conciliacao_forn", "cp_conciliacao", ["fornecedor_id"])

    # ── cp_divergencia ──────────────────────────────────────────────────────────
    op.create_table(
        "cp_divergencia",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fornecedor_id", sa.Integer(), sa.ForeignKey("cp_fornecedor.id"), nullable=True),
        sa.Column("lancamento_id", sa.Integer(), sa.ForeignKey("cp_lancamento.id"), nullable=True),
        sa.Column("tipo", sa.String(50), nullable=False),
        sa.Column("severidade", sa.String(20), nullable=True),
        sa.Column("descricao", sa.Text(), nullable=False),
        sa.Column("valor_esperado", sa.Numeric(15, 2), nullable=True),
        sa.Column("valor_encontrado", sa.Numeric(15, 2), nullable=True),
        sa.Column("diferenca", sa.Numeric(15, 2), nullable=True),
        sa.Column("resolvido", sa.Boolean(), nullable=True, default=False),
        sa.Column("observacao_resolucao", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cp_divergencia_forn", "cp_divergencia", ["fornecedor_id"])
    op.create_index("ix_cp_divergencia_resolvido", "cp_divergencia", ["resolvido"])


def downgrade() -> None:
    op.drop_table("cp_divergencia")
    op.drop_table("cp_conciliacao")
    op.drop_table("cp_lancamento")
    op.drop_table("cp_fornecedor")
    op.drop_table("cp_arquivo")
