"""Marca de cancelamento na partida: quem desfez, quando e por quê.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-25

O cancelamento (0028/fase 01) só marcava `deleted_at`. Isso diz QUE a partida
saiu, mas não quem tirou nem por quê — essa informação existia apenas como
texto dentro de `audit_logs`, que é restrito a admin e guarda JSON serializado.

Sem uma marca estruturada, a tela "Desfeitas" só poderia ser montada casando
`NeoDecisao.motivo` por prefixo de string ("Classificação cancelada: ..."), que
quebra no dia em que alguém reescrever a mensagem.

`deleted_at` continua sendo o soft delete genérico; `cancelado_em` diz que a
remoção foi um cancelamento deliberado de lançamento, e não outra coisa. Os dois
coincidem hoje, mas significam coisas diferentes — e a fase 03 (estorno aditivo)
vai precisar exatamente dessa distinção, já que lá a partida original permanece
ATIVA e ainda assim estornada.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "registros_contabeis",
        sa.Column("cancelado_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "registros_contabeis", sa.Column("cancelado_por", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "registros_contabeis",
        sa.Column("motivo_cancelamento", sa.String(length=300), nullable=True),
    )
    op.create_foreign_key(
        "fk_registro_cancelado_por",
        "registros_contabeis",
        "usuarios",
        ["cancelado_por"],
        ["id"],
    )
    # A tela lista os cancelamentos mais recentes primeiro, por empresa.
    op.create_index(
        "ix_registro_empresa_cancelado",
        "registros_contabeis",
        ["empresa_id", "cancelado_em"],
    )


def downgrade() -> None:
    op.drop_index("ix_registro_empresa_cancelado", table_name="registros_contabeis")
    op.drop_constraint(
        "fk_registro_cancelado_por", "registros_contabeis", type_="foreignkey"
    )
    op.drop_column("registros_contabeis", "motivo_cancelamento")
    op.drop_column("registros_contabeis", "cancelado_por")
    op.drop_column("registros_contabeis", "cancelado_em")
