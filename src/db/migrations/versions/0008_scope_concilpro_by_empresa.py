"""Isola todas as tabelas do ConcilPro por empresa.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07

Dados legados só são atribuídos quando o CNPJ do arquivo identifica exatamente
uma empresa no banco. A migration aborta se restar qualquer registro sem dono
inequívoco: escolher uma empresa arbitrariamente recriaria o vazamento que esta
alteração corrige, enquanto apagar os dados tornaria a migração destrutiva.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


TABELAS = (
    "cp_arquivo",
    "cp_fornecedor",
    "cp_lancamento",
    "cp_conciliacao",
    "cp_divergencia",
)


def upgrade() -> None:
    empresa_uuid = postgresql.UUID(as_uuid=True)
    for tabela in TABELAS:
        op.add_column(tabela, sa.Column("empresa_id", empresa_uuid, nullable=True))

    # O CNPJ extraído do razão é a única informação de ownership disponível no
    # schema legado. Só aceitamos correspondência globalmente inequívoca.
    op.execute(
        """
        UPDATE cp_arquivo AS arquivo
           SET empresa_id = empresa.id
          FROM empresas AS empresa
         WHERE regexp_replace(coalesce(arquivo.cnpj_empresa, ''), '\\D', '', 'g') <> ''
           AND regexp_replace(coalesce(arquivo.cnpj_empresa, ''), '\\D', '', 'g')
               = regexp_replace(empresa.cnpj, '\\D', '', 'g')
           AND (
               SELECT count(*)
                 FROM empresas AS candidata
                WHERE regexp_replace(candidata.cnpj, '\\D', '', 'g')
                      = regexp_replace(coalesce(arquivo.cnpj_empresa, ''), '\\D', '', 'g')
           ) = 1
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM cp_arquivo WHERE empresa_id IS NULL) THEN
                RAISE EXCEPTION
                    'ConcilPro possui arquivos legados sem empresa inequívoca. '
                    'Associe cada cp_arquivo a uma empresa antes de executar a migration 0008.';
            END IF;
        END
        $$
        """
    )

    op.execute(
        """
        UPDATE cp_fornecedor AS fornecedor
           SET empresa_id = arquivo.empresa_id
          FROM cp_arquivo AS arquivo
         WHERE fornecedor.arquivo_origem_id = arquivo.id
        """
    )
    op.execute(
        """
        UPDATE cp_lancamento AS lancamento
           SET empresa_id = fornecedor.empresa_id
          FROM cp_fornecedor AS fornecedor
         WHERE lancamento.fornecedor_id = fornecedor.id
        """
    )
    op.execute(
        """
        UPDATE cp_conciliacao AS conciliacao
           SET empresa_id = fornecedor.empresa_id
          FROM cp_fornecedor AS fornecedor
         WHERE conciliacao.fornecedor_id = fornecedor.id
        """
    )
    op.execute(
        """
        UPDATE cp_divergencia AS divergencia
           SET empresa_id = fornecedor.empresa_id
          FROM cp_fornecedor AS fornecedor
         WHERE divergencia.fornecedor_id = fornecedor.id
        """
    )
    op.execute(
        """
        UPDATE cp_divergencia AS divergencia
           SET empresa_id = lancamento.empresa_id
          FROM cp_lancamento AS lancamento
         WHERE divergencia.empresa_id IS NULL
           AND divergencia.lancamento_id = lancamento.id
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM cp_fornecedor WHERE empresa_id IS NULL)
               OR EXISTS (SELECT 1 FROM cp_lancamento WHERE empresa_id IS NULL)
               OR EXISTS (SELECT 1 FROM cp_conciliacao WHERE empresa_id IS NULL)
               OR EXISTS (SELECT 1 FROM cp_divergencia WHERE empresa_id IS NULL) THEN
                RAISE EXCEPTION
                    'ConcilPro possui registros filhos órfãos; corrija as referências '
                    'antes de executar a migration 0008.';
            END IF;
        END
        $$
        """
    )

    for tabela in TABELAS:
        op.alter_column(tabela, "empresa_id", nullable=False)
        op.create_foreign_key(
            f"fk_{tabela}_empresa_id_empresas",
            tabela,
            "empresas",
            ["empresa_id"],
            ["id"],
        )
        op.create_index(f"ix_{tabela}_empresa", tabela, ["empresa_id"])

    # O hash deixa de ser global e passa a impedir duplicidade só na empresa.
    op.drop_constraint("cp_arquivo_hash_arquivo_key", "cp_arquivo", type_="unique")
    op.create_unique_constraint(
        "uq_cp_arquivo_empresa_hash",
        "cp_arquivo",
        ["empresa_id", "hash_arquivo"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_cp_arquivo_empresa_hash", "cp_arquivo", type_="unique")
    op.create_unique_constraint(
        "cp_arquivo_hash_arquivo_key",
        "cp_arquivo",
        ["hash_arquivo"],
    )

    for tabela in reversed(TABELAS):
        op.drop_index(f"ix_{tabela}_empresa", table_name=tabela)
        op.drop_constraint(
            f"fk_{tabela}_empresa_id_empresas",
            tabela,
            type_="foreignkey",
        )
        op.drop_column(tabela, "empresa_id")
