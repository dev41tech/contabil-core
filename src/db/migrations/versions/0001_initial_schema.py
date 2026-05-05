"""Initial schema — all tables.

Revision ID: 0001
Revises:
Create Date: 2026-04-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Enums
    role_enum = sa.Enum("admin", "contador", name="role_enum")
    plano_enum = sa.Enum("basico", "premium", name="plano_enum")
    regime_enum = sa.Enum("simples_nacional", "lucro_presumido", "lucro_real", name="regime_enum")
    dc_enum = sa.Enum("D", "C", name="dc_enum")
    dc_transacao_enum = sa.Enum("D", "C", name="dc_transacao_enum")
    dc_registro_enum = sa.Enum("D", "C", name="dc_registro_enum")
    tipo_regra_enum = sa.Enum("automatica", "manual", name="tipo_regra_enum")
    status_transacao_enum = sa.Enum("pendente", "processada", "erro", name="status_transacao_enum")
    tipo_nota_enum = sa.Enum("nfe", "nfse", name="tipo_nota_enum")
    status_nota_enum = sa.Enum("pendente", "associada", "cancelada", name="status_nota_enum")
    resultado_neo_enum = sa.Enum("associada", "sem_regra", "erro", name="resultado_neo_enum")
    formato_export_enum = sa.Enum("csv", "xlsx", name="formato_export_enum")
    status_job_enum = sa.Enum("pendente", "processando", "concluido", "erro", name="status_job_enum")

    # ── tenants
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("nome", sa.String(200), nullable=False),
        sa.Column("cnpj", sa.String(18), nullable=False, unique=True),
        sa.Column("plano", plano_enum, nullable=False, server_default="basico"),
        sa.Column("ativo", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── usuarios
    op.create_table(
        "usuarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("nome", sa.String(200), nullable=False),
        sa.Column("senha_hash", sa.String(72), nullable=False),
        sa.Column("role", role_enum, nullable=False, server_default="contador"),
        sa.Column("ativo", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_usuario_tenant_email", "usuarios", ["tenant_id", "email"])
    op.create_index("ix_usuario_email", "usuarios", ["email"])

    # ── refresh_tokens
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("usuario_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("jti", sa.String(64), nullable=False, unique=True),
        sa.Column("revogado", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_refresh_token_jti", "refresh_tokens", ["jti"])

    # ── empresas
    op.create_table(
        "empresas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("razao_social", sa.String(300), nullable=False),
        sa.Column("cnpj", sa.String(18), nullable=False),
        sa.Column("regime_tributario", regime_enum, nullable=False),
        sa.Column("ativa", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_empresa_tenant_cnpj", "empresas", ["tenant_id", "cnpj"])

    # ── permissoes
    op.create_table(
        "permissoes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("usuario_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("usuarios.id"), nullable=False),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("papel", sa.String(50), nullable=False, server_default="contador"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_permissao_usuario_empresa", "permissoes", ["usuario_id", "empresa_id"])

    # ── plano_contas
    op.create_table(
        "plano_contas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("pai_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plano_contas.id"), nullable=True),
        sa.Column("codigo", sa.String(30), nullable=False),
        sa.Column("descricao", sa.String(300), nullable=False),
        sa.Column("tipo", sa.String(50), nullable=False),
        sa.Column("aceita_lancamentos", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_conta_empresa_codigo", "plano_contas", ["empresa_id", "codigo"])

    # ── agencias_bancarias
    op.create_table(
        "agencias_bancarias",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("banco_sigla", sa.String(20), nullable=False),
        sa.Column("agencia", sa.String(10), nullable=False),
        sa.Column("numero", sa.String(20), nullable=False),
        sa.Column("digito", sa.String(5), nullable=True),
        sa.Column("ativa", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_agencia_empresa_banco_agencia_numero",
        "agencias_bancarias",
        ["empresa_id", "banco_sigla", "agencia", "numero"],
    )

    # ── regras
    op.create_table(
        "regras",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("conta_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plano_contas.id"), nullable=False),
        sa.Column("agencia_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agencias_bancarias.id"), nullable=False),
        sa.Column("descricao", sa.String(500), nullable=False),
        sa.Column("historico", sa.String(500), nullable=False),
        sa.Column("dc", dc_enum, nullable=False),
        sa.Column("tipo", tipo_regra_enum, nullable=False),
        sa.Column("manter_historico", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("ativa", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_regra_empresa_agencia_historico", "regras", ["empresa_id", "agencia_id", "historico"]
    )

    # ── transacoes
    op.create_table(
        "transacoes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("agencia_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agencias_bancarias.id"), nullable=False),
        sa.Column("data", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("historico", sa.String(500), nullable=False),
        sa.Column("dc", dc_transacao_enum, nullable=False),
        sa.Column("status", status_transacao_enum, nullable=False, server_default="pendente"),
        sa.Column("hash_dedup", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_transacao_empresa_hash", "transacoes", ["empresa_id", "hash_dedup"])
    op.create_index("ix_transacao_empresa_status", "transacoes", ["empresa_id", "status"])

    # ── registros_contabeis
    op.create_table(
        "registros_contabeis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("transacao_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("transacoes.id"), nullable=True),
        sa.Column("conta_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plano_contas.id"), nullable=False),
        sa.Column("agencia_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agencias_bancarias.id"), nullable=False),
        sa.Column("descricao", sa.String(500), nullable=False),
        sa.Column("historico", sa.String(500), nullable=False),
        sa.Column("historico_extrato", sa.String(500), nullable=False),
        sa.Column("dc", dc_registro_enum, nullable=False),
        sa.Column("tipo_regra", sa.String(50), nullable=False),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("data_lancamento", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_registro_empresa_data", "registros_contabeis", ["empresa_id", "data_lancamento"])

    # ── notas_fiscais
    op.create_table(
        "notas_fiscais",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("tipo", tipo_nota_enum, nullable=False),
        sa.Column("numero", sa.String(50), nullable=False),
        sa.Column("serie", sa.String(10), nullable=True),
        sa.Column("cnpj_emitente", sa.String(18), nullable=False),
        sa.Column("nome_emitente", sa.String(300), nullable=True),
        sa.Column("cnpj_destinatario", sa.String(18), nullable=True),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("data_emissao", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", status_nota_enum, nullable=False, server_default="pendente"),
        sa.Column("transacao_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("transacoes.id"), nullable=True),
        sa.Column("chave_acesso", sa.String(60), nullable=True, unique=True),
        sa.Column("observacao", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_nota_empresa_status", "notas_fiscais", ["empresa_id", "status"])
    op.create_index("ix_nota_empresa_emissao", "notas_fiscais", ["empresa_id", "data_emissao"])

    # ── neo_decisoes
    op.create_table(
        "neo_decisoes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("transacao_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("transacoes.id"), nullable=False),
        sa.Column("regra_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("regras.id"), nullable=True),
        sa.Column("resultado", resultado_neo_enum, nullable=False),
        sa.Column("estrategia", sa.String(50), nullable=True),
        sa.Column("motivo", sa.String(500), nullable=True),
        sa.Column("processado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_neo_empresa_resultado", "neo_decisoes", ["empresa_id", "resultado"])
    op.create_index("ix_neo_transacao", "neo_decisoes", ["transacao_id"])

    # ── export_jobs
    op.create_table(
        "export_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("empresas.id"), nullable=False),
        sa.Column("formato", formato_export_enum, nullable=False),
        sa.Column("status", status_job_enum, nullable=False, server_default="pendente"),
        sa.Column("filtro_de", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filtro_ate", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_registros", sa.Integer, nullable=True),
        sa.Column("arquivo_path", sa.String(500), nullable=True),
        sa.Column("erro_msg", sa.String(500), nullable=True),
        sa.Column("criado_por", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_export_job_empresa", "export_jobs", ["empresa_id", "status"])

    # ── audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("usuario_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("empresa_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("acao", sa.String(100), nullable=False),
        sa.Column("entidade", sa.String(100), nullable=False),
        sa.Column("entidade_id", sa.String(50), nullable=True),
        sa.Column("dados_antes", sa.Text, nullable=True),
        sa.Column("dados_depois", sa.Text, nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_tenant_acao", "audit_logs", ["tenant_id", "acao"])


def downgrade() -> None:
    for tbl in [
        "audit_logs", "export_jobs", "neo_decisoes", "notas_fiscais",
        "registros_contabeis", "transacoes", "regras", "plano_contas",
        "permissoes", "agencias_bancarias", "refresh_tokens", "usuarios",
        "empresas", "tenants",
    ]:
        op.drop_table(tbl)

    for enum in [
        "plano_enum", "dc_enum", "dc_transacao_enum", "dc_registro_enum",
        "tipo_regra_enum", "status_transacao_enum", "tipo_nota_enum",
        "status_nota_enum", "resultado_neo_enum", "formato_export_enum", "status_job_enum",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum}")
    op.execute("DROP TYPE IF EXISTS role_enum")
    op.execute("DROP TYPE IF EXISTS regime_enum")
