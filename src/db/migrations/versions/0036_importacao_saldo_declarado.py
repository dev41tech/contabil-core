"""Lote de importação guarda o saldo de fechamento que o arquivo declara.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-02

O QUE ESTAVA SENDO IGNORADO

Todo OFX traz `<LEDGERBAL>` — o saldo da conta no fim do período. O parser nunca
o leu, e o comentário no código chegava a afirmar que "o OFX não traz essa
informação". É verdade POR LANÇAMENTO (daí `transacoes.saldo_apos` seguir nulo
em tudo que vem de OFX) e falso POR PERÍODO.

POR QUE ISSO IMPORTA

A cadeia de saldos do PDF prova que os valores presentes são consistentes entre
si. Ela **não** prova que nada foi acrescentado nem perdido: lançamento que
entra ou some entre duas âncoras, e que se compensa, passa. Aconteceu duas vezes
em 31/08 e 01/09/2026 — o modelo de visão perdendo cinco lançamentos com a
cadeia fechando, e o `SALDO ANTERIOR` do Sicoob entrando como crédito.

O saldo declarado fecha esse ponto cego, porque é um número que o banco afirma
e que não deriva dos lançamentos. Encadeado entre arquivos consecutivos da mesma
conta — fechamento do anterior + movimento deste = fechamento deste — ele
confere completude de fora.

Medido nos seis extratos Sicoob de jan–jun/2026 da conta 43321-7: os cinco meses
com âncora anterior fecham no centavo.

O QUE MUDA NO BANCO

Três colunas em `extrato_importacoes`, todas nulas:

- `saldo_declarado`      NUMERIC(15,2) — o `<LEDGERBAL>` do arquivo
- `data_saldo_declarado` DATE          — o `<DTASOF>` correspondente
- `alerta_saldo`         VARCHAR(500)  — a frase, quando a conferência não fecha

Mais o índice `(agencia_id, data_saldo_declarado)`, que sustenta a busca da
âncora: o último fechamento declarado desta conta ANTES da data deste arquivo.
Ordenar por data e não por `created_at` é o que faz a conferência sobreviver a
upload fora de ordem — subir junho antes de fevereiro não elege junho como
âncora de fevereiro.

POR QUE NULO E POR QUE AVISO

Nulo é estado permanente, não pendência de backfill: PDF não declara saldo de
período, OFX de outros bancos pode não trazer `LEDGERBAL`, e os lotes antigos
foram importados antes desta leitura existir. Derivar o valor da soma dos
lançamentos já gravados produziria um número que não veio do banco — que é
exatamente a classe de valor plausível-porém-não-conferido que causou o
incidente da SINCOPEÇAS.

E a divergência é AVISO, não recusa. O caso legítimo mais comum é o contador
pular um período: subir fevereiro e depois abril faz a conta não fechar por
março inteiro, e recusar abril travaria o mês por um arquivo que está correto. A
frase nomeia a data da âncora justamente para o buraco ficar visível.

SEGURANÇA DO ALTER

Três `ADD COLUMN` nulos sem default: metadado apenas, sem reescrita de tabela,
instantâneos mesmo com a tabela grande. Nenhuma linha existente é tocada.
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extrato_importacoes",
        sa.Column("saldo_declarado", sa.Numeric(15, 2), nullable=True),
    )
    op.add_column(
        "extrato_importacoes",
        sa.Column("data_saldo_declarado", sa.Date(), nullable=True),
    )
    op.add_column(
        "extrato_importacoes",
        sa.Column("alerta_saldo", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_importacao_agencia_saldo",
        "extrato_importacoes",
        ["agencia_id", "data_saldo_declarado"],
    )


def downgrade() -> None:
    op.drop_index("ix_importacao_agencia_saldo", table_name="extrato_importacoes")
    op.drop_column("extrato_importacoes", "alerta_saldo")
    op.drop_column("extrato_importacoes", "data_saldo_declarado")
    op.drop_column("extrato_importacoes", "saldo_declarado")
