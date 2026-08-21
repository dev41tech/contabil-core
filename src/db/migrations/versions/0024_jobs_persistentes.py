"""Cria jobs persistentes para NEO e importação de extrato.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    job_tipo = sa.Enum("neo_processar", "extrato_importar", name="job_tipo_enum")
    job_status = sa.Enum(
        "na_fila",
        "processando",
        "concluido",
        "concluido_com_alertas",
        "falhou",
        name="job_status_enum",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("empresa_id", sa.Uuid(), nullable=True),
        sa.Column("tipo", job_tipo, nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="na_fila"),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("processados", sa.Integer(), nullable=True),
        sa.Column("resultado", sa.JSON(), nullable=True),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("criado_por", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("iniciado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("concluido_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_em", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_empresa_created", "jobs", ["empresa_id", "created_at"])
    op.create_index("ix_jobs_status_heartbeat", "jobs", ["status", "heartbeat_em"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_heartbeat", table_name="jobs")
    op.drop_index("ix_jobs_empresa_created", table_name="jobs")
    op.drop_table("jobs")
    postgresql.ENUM(name="job_status_enum").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="job_tipo_enum").drop(op.get_bind(), checkfirst=True)
