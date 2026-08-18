"""Adiciona 'txt' ao enum formato_export_enum.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-18

Item pedido pelo escritório: exportação de `lancamentos_importacao` em .txt,
no layout exato do arquivo modelo deles (`src/domain/exportacao/service.py`).
A validação em Python já aceitava 'txt', mas a coluna `export_jobs.formato` é
um ENUM nativo do Postgres restrito a ('csv', 'xlsx') — faltava esta migration
para o banco aceitar o valor novo. Sem ela, toda exportação em .txt falhava
com 500 ao tentar gravar o ExportJob (InvalidTextRepresentationError).

Postgres não permite remover um valor de enum (não existe `DROP VALUE`), então
o downgrade é um no-op documentado — não há como desfazer isto de forma limpa.
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: seguro para reexecutar caso um deploy anterior já tenha
    # aplicado só este trecho antes de falhar em outro lugar.
    op.execute("ALTER TYPE formato_export_enum ADD VALUE IF NOT EXISTS 'txt'")


def downgrade() -> None:
    # Postgres não suporta remover valor de enum — reverter exigiria recriar
    # o tipo inteiro e todas as colunas que o usam. Não implementado porque
    # nunca precisamos desfazer isto na prática (ninguém volta a proibir um
    # formato de exportação já liberado).
    pass
