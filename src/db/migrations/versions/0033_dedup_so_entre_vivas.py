"""Dedup de transação passa a valer só entre linhas vivas.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-28

O PROBLEMA

`cancelar_importacao` faz soft delete: marca `transacoes.deleted_at` e mantém a
linha. Mas a unicidade era total — `uq_transacao_empresa_hash (empresa_id,
hash_dedup)` — então o hash da linha cancelada continuava ocupando o lugar.
Consequência: **arquivo cancelado nunca mais podia ser importado.**

Isso quebra justamente o fluxo de conserto do escritório: subiu o extrato
errado, cancela, sobe o certo — e o segundo upload volta com zero lançamentos.
Pior, volta em silêncio: as linhas são contadas como "duplicadas", que é a
mesma mensagem de um reenvio legítimo. Ninguém tem como distinguir "já estava
lá" de "está bloqueado por uma linha que eu mesmo cancelei".

Atinge os três caminhos que gravam transação: importação de OFX, importação de
PDF e a sincronização do Open Banking — esta última pior ainda, porque roda
sozinha e ninguém está olhando quando ela devolve zero.

O CONSERTO

Índice único parcial `WHERE deleted_at IS NULL`, o mesmo padrão que
`uq_registro_transacao_dc_ativo` já usa nesta base. A linha cancelada continua
no banco, com o rastro de quem cancelou e por quê, mas deixa de reservar o
hash.

POR QUE NÃO HARD DELETE NO CANCELAMENTO

Era a alternativa e foi descartada: `registros_contabeis.transacao_id` é FK
para `transacoes.id`. Apagar a transação de verdade ou deixaria a partida órfã
no razão — sem transação para explicá-la — ou exigiria cascade, que apagaria
registro contábil de período possivelmente já fechado. O soft delete existe por
esse motivo; o defeito nunca foi ele, era a unicidade não acompanhá-lo.

SEGURANÇA DO ALTER

Não há como esta migration falhar por dado existente: a constraint antiga já
impedia duas linhas com o mesmo `(empresa_id, hash_dedup)`, então o conjunto
que sobra sob `WHERE deleted_at IS NULL` é, no máximo, o mesmo — nunca maior.
Nenhuma linha é reescrita.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_transacao_empresa_hash", "transacoes", type_="unique")
    op.create_index(
        "uq_transacao_empresa_hash_ativo",
        "transacoes",
        ["empresa_id", "hash_dedup"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Volta à unicidade total.

    Só é seguro enquanto não houver hash repetido entre uma linha viva e uma
    apagada — situação que o próprio conserto passa a permitir. Se o downgrade
    falhar por violação de unicidade, é exatamente isso: existe extrato
    reimportado depois de um cancelamento, e voltar atrás significaria escolher
    qual das duas linhas descartar. Não é decisão de migration.
    """
    op.drop_index("uq_transacao_empresa_hash_ativo", table_name="transacoes")
    op.create_unique_constraint(
        "uq_transacao_empresa_hash", "transacoes", ["empresa_id", "hash_dedup"]
    )
