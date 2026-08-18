"""Add contrapartes table.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-17

Item 1 do PDF "Alteracoes no sistema" (feedback dos contadores): "seria
possível o sistema tentar localizar automaticamente o fornecedor" por
NF/CNPJ/Razão Social/Nome Fantasia e já vincular a conta contábil. Não
reaproveita `Regra` (gatilho textual por agência) nem `CpFornecedor` (razão
de fornecedores do ConcilPro, isolado) — cadastro novo, dedicado a identidade
fiscal + conta padrão, cobrindo fornecedor e cliente (`tipo`) porque o mesmo
documento aparece nos dois sentidos conforme o histórico é pagamento ou
recebimento.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

tipo_contraparte_enum = postgresql.ENUM(
    "fornecedor", "cliente", "ambos", name="tipo_contraparte_enum"
)
origem_contraparte_enum = postgresql.ENUM(
    "manual", "nota_fiscal", "comprovante", "historico_extrato", "backfill",
    name="origem_contraparte_enum",
)


def upgrade() -> None:
    bind = op.get_bind()
    tipo_contraparte_enum.create(bind, checkfirst=True)
    origem_contraparte_enum.create(bind, checkfirst=True)

    op.create_table(
        "contrapartes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id"),
            nullable=False,
        ),
        sa.Column("tipo", tipo_contraparte_enum, nullable=False),
        sa.Column("documento", sa.String(14), nullable=False),
        sa.Column("razao_social", sa.String(300), nullable=False),
        sa.Column("nome_fantasia", sa.String(300), nullable=True),
        sa.Column(
            "conta_contabil_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plano_contas.id"),
            nullable=False,
        ),
        sa.Column(
            "origem", origem_contraparte_enum, nullable=False, server_default="manual"
        ),
        sa.Column("confirmado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "confirmado_por",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usuarios.id"),
            nullable=True,
        ),
        sa.Column("ativa", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("contrapartes", "origem", server_default=None)

    op.create_index(
        "uq_contraparte_empresa_documento_ativa",
        "contrapartes",
        ["empresa_id", "documento"],
        unique=True,
        postgresql_where=sa.text("ativa = true AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_contraparte_empresa_razao_social",
        "contrapartes",
        ["empresa_id", "razao_social"],
    )


def downgrade() -> None:
    op.drop_index("ix_contraparte_empresa_razao_social", table_name="contrapartes")
    op.drop_index("uq_contraparte_empresa_documento_ativa", table_name="contrapartes")
    op.drop_table("contrapartes")
    origem_contraparte_enum.drop(op.get_bind(), checkfirst=True)
    tipo_contraparte_enum.drop(op.get_bind(), checkfirst=True)
