"""Adiciona a coluna modulos que faltava em permissoes.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-10

O model `Permissao.modulos` (achado A1 da auditoria de 06/08/2026) foi
adicionado ao código sem a migration correspondente — os testes não pegaram
porque rodam contra um schema criado direto do model (`Base.metadata.create_all`
em SQLite), não pelas migrations Alembic. Em produção, qualquer contador batendo
em `get_company_context` derrubava a requisição com
`UndefinedColumnError: column permissoes.modulos does not exist`.

Backfill com '*' para linhas existentes: antes dessa coluna existir, a mera
presença de uma linha em `permissoes` já significava acesso total à empresa —
'*' preserva esse comportamento para permissões já concedidas.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "permissoes",
        sa.Column("modulos", sa.String(500), nullable=False, server_default="*"),
    )
    op.alter_column("permissoes", "modulos", server_default=None)


def downgrade() -> None:
    op.drop_column("permissoes", "modulos")
