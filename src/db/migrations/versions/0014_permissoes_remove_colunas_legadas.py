"""Alinha o schema de permissoes ao model (remove colunas legadas).

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-10

O model `Permissao` (redesenhado no achado A1 da auditoria de 06/08/2026)
usa `usuario_id` + `empresa_id` como chave primária composta e só conhece
a coluna `modulos`. A tabela real, criada pela migration 0001, nunca foi
migrada para acompanhar: ainda tem `id` (chave primária antiga),
`papel` (substituída por `modulos`), `created_at`, `updated_at` e
`deleted_at` — nenhuma dessas colunas é lida ou escrita pelo código atual.

`id`, `created_at` e `updated_at` são NOT NULL sem valor padrão no banco
(dependiam do model antigo preenchê-las em Python). Como o INSERT gerado
pelo model atual não menciona essas colunas, toda concessão de permissão
pelo sistema quebra com violação de NOT NULL. Isso é anterior à auditoria
— só ficou visível agora que o deploy finalmente chegou até esse código.

`usuario_id` + `empresa_id` já têm a unique constraint
`uq_permissao_usuario_empresa` desde a migration 0001 — vira a chave
primária de verdade, exatamente como o model já assume.

Confirmado antes de escrever esta migration: nenhum código ou FK
referencia `permissoes.id`.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("permissoes_pkey", "permissoes", type_="primary")
    op.drop_column("permissoes", "id")
    op.drop_column("permissoes", "papel")
    op.drop_column("permissoes", "created_at")
    op.drop_column("permissoes", "updated_at")
    op.drop_column("permissoes", "deleted_at")
    op.drop_constraint("uq_permissao_usuario_empresa", "permissoes", type_="unique")
    op.create_primary_key(
        "permissoes_pkey", "permissoes", ["usuario_id", "empresa_id"]
    )


def downgrade() -> None:
    import sqlalchemy as sa

    op.drop_constraint("permissoes_pkey", "permissoes", type_="primary")
    op.add_column(
        "permissoes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "permissoes",
        sa.Column(
            "papel", sa.String(50), nullable=False, server_default="contador"
        ),
    )
    op.add_column(
        "permissoes",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "permissoes",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "permissoes",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE permissoes SET id = gen_random_uuid()")
    op.alter_column("permissoes", "id", nullable=False)
    op.create_primary_key("permissoes_pkey", "permissoes", ["id"])
    op.create_unique_constraint(
        "uq_permissao_usuario_empresa", "permissoes", ["usuario_id", "empresa_id"]
    )
