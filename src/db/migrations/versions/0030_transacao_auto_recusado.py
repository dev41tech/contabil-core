"""A transação lembra que um humano recusou a classificação automática.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-25

Item 4.3 do relatório de melhorias: "a conciliação manual deve ter prioridade
sobre as automáticas".

Hoje ela não tem. Desfazer devolve a transação para `pendente`, e a fila do
motor só olha `status` e `deleted_at` — então a MESMA regra que classificou
reclassifica na execução seguinte, do mesmo jeito. O contador desfaz, roda o
NEO, e volta ao ponto de partida. Foi exatamente o que aconteceu com a regra
`TARIFA BAIXA DE TITULOS` em 25/08.

O que faltava era memória de que um humano discordou. `auto_recusado_em` é essa
memória: enquanto estiver preenchido, o motor não classifica aquela transação
sozinho — ela espera decisão humana.

POR QUE BLOQUEAR A TRANSAÇÃO E NÃO A REGRA

Desativar a regra seria largo demais: ela pode estar certa para dezenas de
outros lançamentos e errada só neste. Guardar o par (transação, regra) recusada
seria mais fino, mas deixaria OUTRA regra tentar a sorte — e o humano não disse
"esta regra errou", disse "a máquina errou aqui". Deixar outra regra chutar
seria a máquina insistindo com outro palpite.

O bloqueio é reversível: quem desfez por engano libera a transação de volta para
o automático, sem precisar mexer em regra nenhuma.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transacoes",
        sa.Column("auto_recusado_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "transacoes", sa.Column("auto_recusado_por", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_transacao_auto_recusado_por",
        "transacoes",
        "usuarios",
        ["auto_recusado_por"],
        ["id"],
    )
    # A fila do motor filtra por (empresa, status) e agora também por este campo.
    op.create_index(
        "ix_transacao_empresa_status_auto",
        "transacoes",
        ["empresa_id", "status", "auto_recusado_em"],
    )


def downgrade() -> None:
    op.drop_index("ix_transacao_empresa_status_auto", table_name="transacoes")
    op.drop_constraint(
        "fk_transacao_auto_recusado_por", "transacoes", type_="foreignkey"
    )
    op.drop_column("transacoes", "auto_recusado_por")
    op.drop_column("transacoes", "auto_recusado_em")
