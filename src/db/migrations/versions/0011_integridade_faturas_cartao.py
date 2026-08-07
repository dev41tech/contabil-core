"""Protege quitacoes de fatura duplicadas.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_fatura_transacao",
        "faturas_cartao",
        ["transacao_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_fatura_transacao",
        "faturas_cartao",
        type_="unique",
    )
