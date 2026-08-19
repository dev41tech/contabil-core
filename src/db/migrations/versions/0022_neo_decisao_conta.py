"""Registra a conta contábil diretamente na decisão do NEO.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-19

Até aqui a conta era inferida pela regra ao listar decisões. Isso apagava do
filtro as classificações manuais e por contraparte, que não possuem `regra_id`.
O backfill copia somente a conta de decisões cuja regra ainda permite uma
inferência segura; decisões antigas sem regra permanecem nulas de propósito.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "neo_decisoes",
        sa.Column(
            "conta_id",
            sa.Uuid(),
            sa.ForeignKey("plano_contas.id"),
            nullable=True,
        ),
    )
    # A subconsulta correlacionada funciona em PostgreSQL e SQLite e evita
    # escolher uma conta quando a decisão não guarda uma regra verificável.
    op.execute(
        """
        UPDATE neo_decisoes
        SET conta_id = (
            SELECT regras.conta_id
            FROM regras
            WHERE regras.id = neo_decisoes.regra_id
        )
        WHERE regra_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("neo_decisoes", "conta_id")
