"""Persiste o saldo corrente lido do extrato em cada transação.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-21

O parser de PDF sempre leu a coluna de saldo — e sempre a descartou. O contador
confere lançamento contra o saldo linha a linha, e é assim que ele percebe
transação faltando ou valor trocado; sem essa coluna a conferência não existe no
sistema. O OFX não traz saldo, então o PDF é a única origem que a preenche.

NULL é estado legítimo e permanente, não pendência de backfill:
  - transações importadas por OFX nunca terão saldo;
  - layouts de PDF sem coluna de saldo também não;
  - as linhas já existentes foram importadas antes desta coluna.

Por isso não há backfill: inventar saldo a partir da soma dos movimentos
produziria um número que não veio do banco, e é exatamente esse tipo de valor
plausível-porém-não-conferido que originou o incidente que motivou este trabalho.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transacoes",
        sa.Column("saldo_apos", sa.Numeric(15, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transacoes", "saldo_apos")
