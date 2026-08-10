"""Idempotência e partidas dobradas do motor NEO.

Revision ID: 0008_neo_motor
Revises: 0007
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agencias_bancarias",
        sa.Column("conta_contabil_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agencia_conta_contabil",
        "agencias_bancarias",
        "plano_contas",
        ["conta_contabil_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_agencias_bancarias_conta_contabil_id",
        "agencias_bancarias",
        ["conta_contabil_id"],
    )
    # Materializa uma conta patrimonial por agência para que registros já
    # existentes também possam receber sua contrapartida.
    op.execute(
        """
        INSERT INTO plano_contas (
            id, empresa_id, codigo, descricao, tipo, tipo_sa,
            created_at, updated_at, deleted_at
        )
        SELECT md5(a.id::text || '-conta-contabil')::uuid,
               a.empresa_id,
               '1.1.B.' || left(replace(a.id::text, '-', ''), 16),
               left(
                   'Conta bancária ' || a.banco_sigla || ' ' || a.agencia || ' ' || a.numero,
                   300
               ),
               'ativo',
               'A',
               CURRENT_TIMESTAMP,
               CURRENT_TIMESTAMP,
               NULL
          FROM agencias_bancarias a
         WHERE a.deleted_at IS NULL
           AND a.conta_contabil_id IS NULL
           AND NOT EXISTS (
               SELECT 1
                 FROM plano_contas p
                WHERE p.empresa_id = a.empresa_id
                  AND p.codigo = '1.1.B.' || left(replace(a.id::text, '-', ''), 16)
           )
        """
    )
    op.execute(
        """
        UPDATE agencias_bancarias a
           SET conta_contabil_id = p.id
          FROM plano_contas p
         WHERE a.conta_contabil_id IS NULL
           AND p.empresa_id = a.empresa_id
           AND p.codigo = '1.1.B.' || left(replace(a.id::text, '-', ''), 16)
        """
    )

    op.add_column(
        "regras",
        sa.Column("historico_normalizado", sa.String(500), nullable=True),
    )
    op.execute(
        "UPDATE regras SET historico_normalizado = lower(btrim(historico))"
    )
    # Se já houver variantes por caixa, a regra ativa mais recente representa a
    # última intenção do usuário; as anteriores são desativadas, não apagadas.
    op.execute(
        """
        WITH duplicadas AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY empresa_id, agencia_id, historico_normalizado
                       ORDER BY ativa DESC, created_at DESC, id DESC
                   ) AS ordem
              FROM regras
             WHERE deleted_at IS NULL
        )
        UPDATE regras
           SET ativa = false
         WHERE id IN (SELECT id FROM duplicadas WHERE ordem > 1)
           AND ativa = true
        """
    )
    op.alter_column("regras", "historico_normalizado", nullable=False)
    op.drop_constraint(
        "uq_regra_empresa_agencia_historico", "regras", type_="unique"
    )
    op.create_index(
        "uq_regra_empresa_agencia_historico_normalizado_ativa",
        "regras",
        ["empresa_id", "agencia_id", "historico_normalizado"],
        unique=True,
        postgresql_where=sa.text("ativa = true AND deleted_at IS NULL"),
    )

    op.add_column(
        "registros_contabeis",
        sa.Column("lancamento_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE registros_contabeis
           SET lancamento_id = COALESCE(transacao_id, id)
         WHERE lancamento_id IS NULL
        """
    )
    op.alter_column("registros_contabeis", "lancamento_id", nullable=False)
    op.create_index(
        "ix_registro_lancamento", "registros_contabeis", ["lancamento_id"]
    )
    # Preserva para auditoria as duplicatas produzidas pelo bug, mas as retira
    # dos livros antes de ativar a chave de idempotência.
    op.execute(
        """
        WITH duplicados AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY transacao_id, dc
                       ORDER BY created_at ASC, id ASC
                   ) AS ordem
              FROM registros_contabeis
             WHERE transacao_id IS NOT NULL
               AND deleted_at IS NULL
        )
        UPDATE registros_contabeis
           SET deleted_at = CURRENT_TIMESTAMP
         WHERE id IN (SELECT id FROM duplicados WHERE ordem > 1)
        """
    )
    op.execute(
        """
        INSERT INTO registros_contabeis (
            id, empresa_id, transacao_id, lancamento_id, conta_id, agencia_id,
            descricao, historico, historico_extrato, dc, tipo_regra, valor,
            data_lancamento, created_at, updated_at, deleted_at
        )
        SELECT md5(r.id::text || '-contrapartida')::uuid,
               r.empresa_id,
               r.transacao_id,
               r.lancamento_id,
               a.conta_contabil_id,
               r.agencia_id,
               left('Contrapartida bancária: ' || r.descricao, 500),
               r.historico,
               r.historico_extrato,
               CASE WHEN r.dc = 'D' THEN 'C'::dc_registro_enum
                    ELSE 'D'::dc_registro_enum END,
               r.tipo_regra,
               r.valor,
               r.data_lancamento,
               CURRENT_TIMESTAMP,
               CURRENT_TIMESTAMP,
               NULL
          FROM registros_contabeis r
          JOIN agencias_bancarias a ON a.id = r.agencia_id
         WHERE r.transacao_id IS NOT NULL
           AND r.deleted_at IS NULL
           AND a.conta_contabil_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
                 FROM registros_contabeis contraparte
                WHERE contraparte.transacao_id = r.transacao_id
                  AND contraparte.dc <> r.dc
                  AND contraparte.deleted_at IS NULL
           )
        """
    )
    op.create_index(
        "uq_registro_transacao_dc_ativo",
        "registros_contabeis",
        ["transacao_id", "dc"],
        unique=True,
        postgresql_where=sa.text(
            "transacao_id IS NOT NULL AND deleted_at IS NULL"
        ),
    )

    op.execute(
        """
        DELETE FROM neo_decisoes
         WHERE id IN (
             SELECT id
               FROM (
                   SELECT id,
                          row_number() OVER (
                              PARTITION BY transacao_id
                              ORDER BY processado_em ASC, id ASC
                          ) AS ordem
                     FROM neo_decisoes
                    WHERE resultado = 'sem_regra'
               ) AS repetidas
              WHERE ordem > 1
         )
        """
    )
    op.create_index(
        "uq_neo_sem_regra_transacao",
        "neo_decisoes",
        ["transacao_id"],
        unique=True,
        postgresql_where=sa.text("resultado = 'sem_regra'"),
    )


def downgrade() -> None:
    op.drop_index("uq_neo_sem_regra_transacao", table_name="neo_decisoes")
    op.drop_index(
        "uq_registro_transacao_dc_ativo", table_name="registros_contabeis"
    )
    op.drop_index("ix_registro_lancamento", table_name="registros_contabeis")
    op.drop_column("registros_contabeis", "lancamento_id")

    op.drop_index(
        "uq_regra_empresa_agencia_historico_normalizado_ativa",
        table_name="regras",
    )
    op.create_unique_constraint(
        "uq_regra_empresa_agencia_historico",
        "regras",
        ["empresa_id", "agencia_id", "historico"],
    )
    op.drop_column("regras", "historico_normalizado")

    op.drop_constraint(
        "uq_agencias_bancarias_conta_contabil_id",
        "agencias_bancarias",
        type_="unique",
    )
    op.drop_constraint(
        "fk_agencia_conta_contabil", "agencias_bancarias", type_="foreignkey"
    )
    op.drop_column("agencias_bancarias", "conta_contabil_id")
