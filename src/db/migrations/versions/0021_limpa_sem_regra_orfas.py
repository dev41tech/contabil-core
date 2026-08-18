"""Remove decisões 'sem_regra' de transações que já foram contabilizadas.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-18

Até a correção de hoje, o NEO inseria uma linha nova em `neo_decisoes` a cada
execução: uma transação que caía em 'sem_regra' e era classificada numa
reexecução ficava com as duas linhas — a 'associada' (correta) e a 'sem_regra'
(órfã). A tela "Sem Regra" filtra por `resultado = 'sem_regra'`, então ela
continuava listando lançamentos já contabilizados, e associá-los respondia
"A transação já foi contabilizada ou não está mais pendente".

O motor agora encerra a decisão aberta em vez de deixar a antiga para trás
(`NeoEngine._registrar_decisao`); esta migration limpa o passivo que já está
no banco. Só apaga linhas 'sem_regra' cuja transação não está mais pendente —
ou seja, linhas que já estavam factualmente erradas. Nenhuma decisão de
transação pendente é tocada.
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM neo_decisoes
        WHERE resultado = 'sem_regra'
          AND transacao_id IN (
              SELECT id FROM transacoes WHERE status <> 'pendente'
          )
        """
    )


def downgrade() -> None:
    # Não há como recriar linhas que descrevem um estado que já não existe —
    # e recriá-las traria de volta exatamente o bug que esta migration corrige.
    pass
