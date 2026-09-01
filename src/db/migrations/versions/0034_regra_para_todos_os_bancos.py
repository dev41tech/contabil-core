"""Regra de categorização pode valer para TODOS os bancos.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-01

O PEDIDO

Ao criar uma regra, a agência é obrigatória. Só que a maior parte das regras do
escritório não depende de banco nenhum: "TARIFA PACOTE DE SERVICOS" vai para a
mesma conta contábil venha do BB, da Caixa ou do Itaú. Hoje isso obriga a
cadastrar a MESMA regra uma vez por agência — e a refazer todas quando a conta
contábil muda.

O CONSERTO

`regras.agencia_id` passa a aceitar NULL, e NULL significa "vale para qualquer
agência desta empresa".

O ÍNDICE ÚNICO PRECISA DE UM IRMÃO

`uq_regra_empresa_agencia_historico_normalizado_ativa` cobre
(empresa_id, agencia_id, historico_normalizado). Em Postgres, dois NULL são
DISTINTOS num índice único — então, sem um segundo índice, nada impediria duas
regras "todos os bancos" com o mesmo histórico e contas contábeis diferentes.
Seriam duas regras concorrendo pela mesma transação, e qual venceria dependeria
da ordem de leitura.

Por isso `uq_regra_empresa_historico_normalizado_global`, com
`WHERE agencia_id IS NULL`, faz o mesmo trabalho para o escopo global.

Uma regra global e uma regra de agência com o MESMO histórico continuam
permitidas de propósito: é o padrão "regra geral mais exceção", e é o motivo de
existirem dois índices em vez de um sobre `COALESCE`.

SEGURANÇA DO ALTER

`ALTER COLUMN ... DROP NOT NULL` não reescreve linha nem valida dado existente:
toda regra que existe hoje tem agência preenchida e continua exatamente como
está. O índice novo nasce vazio, porque nenhuma linha tem `agencia_id IS NULL`
antes desta migration.

O downgrade é que pode falhar, e deve: ver a nota nele.
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "regras",
        "agencia_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_index(
        "uq_regra_empresa_historico_normalizado_global",
        "regras",
        ["empresa_id", "historico_normalizado"],
        unique=True,
        postgresql_where=sa.text(
            "agencia_id IS NULL AND ativa = true AND deleted_at IS NULL"
        ),
        sqlite_where=sa.text(
            "agencia_id IS NULL AND ativa = 1 AND deleted_at IS NULL"
        ),
    )


def downgrade() -> None:
    """Volta a exigir agência — e falha se houver regra global cadastrada.

    Não há resposta automática para uma regra que vale para todos os bancos
    quando a coluna volta a ser obrigatória: escolher uma agência seria inventar
    escopo, e apagar a regra seria descartar uma decisão do contador. A migration
    para com erro de NOT NULL, e a saída é humana — reescrever ou remover essas
    regras antes de descer.
    """
    op.drop_index("uq_regra_empresa_historico_normalizado_global", table_name="regras")
    op.alter_column(
        "regras",
        "agencia_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
