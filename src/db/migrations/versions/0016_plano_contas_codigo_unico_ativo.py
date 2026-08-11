"""Restringe a unicidade de código do plano de contas às linhas ativas.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-11

A constraint original (`uq_conta_empresa_codigo`, criada em 0001) é global —
inclui contas soft-deletadas. "Excluir Todas" no plano de contas faz soft
delete, então reimportar o mesmo plano depois de uma exclusão em massa
esbarra em "duplicate key" para cada código que já existiu, mesmo já
excluído. A unicidade deve valer só entre contas ativas.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_conta_empresa_codigo", "plano_contas", type_="unique")
    op.create_index(
        "uq_plano_empresa_codigo_ativo",
        "plano_contas",
        ["empresa_id", "codigo"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_plano_empresa_codigo_ativo", table_name="plano_contas")
    op.create_unique_constraint(
        "uq_conta_empresa_codigo", "plano_contas", ["empresa_id", "codigo"]
    )
