"""Guarda a posição da linha no arquivo, para desempatar lançamentos do mesmo dia.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-24

A data passou a ser data de calendário (0026), e um extrato tem vários
lançamentos no mesmo dia. O desempate era `id`, que é UUID aleatório — dentro de
um dia a lista saía embaralhada em relação ao extrato do banco.

Ordenar por saldo não resolve: em dia só de débitos o saldo cai, em dia com
crédito ele sobe, então nem crescente nem decrescente serve. Ordenar por valor
também não — o extrato não é ordenado por valor.

A ordem certa é a do próprio arquivo, que o parser lê de cima para baixo e
descartava. Exemplo real (SINCOPEÇAS, 02/01/2026), em que a cadeia de saldo
confirma a sequência:

    TARIFA BAIXA DE TITULOS      -0,20   saldo 67.201,27
    TRANSF ENTRE CONTAS      -6.593,62   saldo 60.607,65
    PAGAMENTO PIX            -2.164,48   saldo 58.443,17

`ordem` guarda essa posição. Só o valor RELATIVO dentro de um mesmo dia importa:
`data` continua sendo o critério primário, e `ordem` só desempata.

BACKFILL

As linhas já importadas recebem a ordem reconstruída de `created_at`. O serviço
insere as transações na sequência em que o parser as leu, e o `created_at` é
gerado por linha no Python, com resolução de microssegundos — então a ordem de
criação preserva a ordem do arquivo. Conferido em produção antes de escrever
isto: os três lançamentos do exemplo acima têm `created_at` .693085, .693096 e
.693101, na ordem do PDF.

É uma única instrução com função de janela, não atualização linha a linha.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transacoes", sa.Column("ordem", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE transacoes t
           SET ordem = r.posicao
        FROM (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY empresa_id, agencia_id, data
                       ORDER BY created_at, id
                   ) AS posicao
            FROM transacoes
        ) r
        WHERE r.id = t.id
        """
    )
    op.create_index(
        "ix_transacao_empresa_data_ordem",
        "transacoes",
        ["empresa_id", "data", "ordem"],
    )


def downgrade() -> None:
    op.drop_index("ix_transacao_empresa_data_ordem", table_name="transacoes")
    op.drop_column("transacoes", "ordem")
