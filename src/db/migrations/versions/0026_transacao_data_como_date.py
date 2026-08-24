"""Data da transação vira data de calendário, não instante.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-24

A coluna era `timestamptz` e as duas origens gravavam semânticas diferentes
nela: o parser de PDF gravava meia-noite UTC como marcador de data, o OFX
gravava um instante real com o offset do banco. Quem lia decidia o fuso por
conta própria — o export formatava as partes em UTC e acertava, a tela
renderizava em horário de Brasília e mostrava sempre um dia a menos, porque
meia-noite UTC é 21h do dia anterior em Brasília.

Data de lançamento bancário é data de calendário: não tem hora, não tem fuso, e
não deve ser reinterpretada por nenhum consumidor. `Date` remove a ambiguidade
na raiz — e, de quebra, faz `data <= :fim` voltar a incluir o último dia do
período, que antes era truncado para 00:00 e descartava o dia inteiro.

Conversão: `AT TIME ZONE 'UTC'` antes do cast preserva a semântica do parser de
PDF, que é a origem da maioria esmagadora das linhas. Lançamentos de OFX
gravados com hora real depois das 21h (BRT) podem ter sido registrados no dia
seguinte em UTC e, nesses casos, o cast mantém o dia deslocado — não há como
recuperar o offset original, que a coluna não guardou. Daqui em diante o
problema não se repete: o parser de OFX resolve a data no fuso declarado pelo
próprio arquivo, antes de persistir.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "transacoes",
        "data",
        type_=sa.Date(),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="(data AT TIME ZONE 'UTC')::date",
    )


def downgrade() -> None:
    # Volta a instante em meia-noite UTC — a hora original não é recuperável.
    op.alter_column(
        "transacoes",
        "data",
        type_=sa.DateTime(timezone=True),
        existing_type=sa.Date(),
        existing_nullable=False,
        postgresql_using="data::timestamptz",
    )
