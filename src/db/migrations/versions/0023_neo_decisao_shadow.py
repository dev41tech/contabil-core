"""Persiste a resolução de contraparte executada em shadow mode pelo NEO.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-20

Os campos pertencem à decisão porque há no máximo uma resolução sombra por
decisão. Não há backfill: NULL em ``conta_divergente`` significa que o shadow
não foi avaliado (sem evidência ou decisão não originada de regra), enquanto
FALSE significa que ele rodou e as contas concordaram. Inventar FALSE para o
histórico anterior apagaria justamente essa distinção.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "neo_decisoes",
        sa.Column(
            "contraparte_id",
            sa.Uuid(),
            sa.ForeignKey("contrapartes.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "neo_decisoes",
        sa.Column(
            "conta_contraparte_id",
            sa.Uuid(),
            sa.ForeignKey("plano_contas.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "neo_decisoes",
        sa.Column("origem_evidencia", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "neo_decisoes",
        sa.Column("conta_divergente", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("neo_decisoes", "conta_divergente")
    op.drop_column("neo_decisoes", "origem_evidencia")
    op.drop_column("neo_decisoes", "conta_contraparte_id")
    op.drop_column("neo_decisoes", "contraparte_id")
