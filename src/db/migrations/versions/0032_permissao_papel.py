"""A membership passa a ter papel: dono, contador ou leitura.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-27

A tabela `permissoes` sempre foi a membership (usuário × empresa). Faltava a
segunda dimensão: `modulos` diz ONDE a pessoa entra, `papel` diz O QUANTO ela
faz lá dentro. O efetivo é a interseção — ver `PAPEIS` em
`src/core/permissoes.py`.

POR QUE TODO MUNDO NASCE `dono`

Backfill conservador de propósito: migration não tira acesso de ninguém. Quem
hoje tem `modulos="plano_contas"` consegue excluir conta, e continuará
conseguindo depois do deploy. O que muda é que agora existe a ferramenta para
tirar — deliberadamente, um usuário por vez.

O padrão de CONCESSÃO NOVA é `contador`, não `dono`: quem for cadastrado a
partir daqui recebe o papel operacional, sem exclusão estrutural nem
importação em massa. Por isso o `server_default` é removido logo depois do
backfill: default de schema silencioso é como se dá acesso demais sem
ninguém decidir. Quem insere passa a ser obrigado a dizer o papel.

POR QUE `String` + CHECK, E NÃO ENUM

Acrescentar `cliente_leitura` depois é uma linha na constraint, contra um
`ALTER TYPE` numa tabela em produção. E enum nomeado neste projeto já custou um
endpoint quebrado (ver 0031).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_PAPEIS = ("dono", "contador", "leitura")


def upgrade() -> None:
    # `server_default` preenche as linhas existentes na própria criação da
    # coluna — sem UPDATE em tabela cheia, e sem janela em que a coluna exista
    # nula.
    op.add_column(
        "permissoes",
        sa.Column("papel", sa.String(20), nullable=False, server_default="dono"),
    )
    op.create_check_constraint(
        "ck_permissao_papel",
        "permissoes",
        "papel IN ('dono', 'contador', 'leitura')",
    )
    # Fora o default do schema: a partir daqui, quem insere diz o papel.
    op.alter_column("permissoes", "papel", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_permissao_papel", "permissoes", type_="check")
    op.drop_column("permissoes", "papel")
