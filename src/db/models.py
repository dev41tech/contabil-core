"""Models SQLAlchemy — todas as tabelas da aplicação.

Regras:
- Todo model tem id (UUID), created_at, updated_at.
- Soft delete via deleted_at (NULL = ativo).
- Multi-tenancy: toda tabela de domínio tem tenant_id FK.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from src.db.session import Base


# ─────────────────────────────────────────────────────────────── Mixin


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ─────────────────────────────────────────────────────────────── Tenant / Escritório


class Tenant(Base, TimestampMixin):
    """Escritório contábil (tenant raiz)."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    cnpj: Mapped[str] = mapped_column(String(18), unique=True, nullable=False)
    plano: Mapped[str] = mapped_column(
        Enum("basico", "premium", name="plano_enum"), default="basico", nullable=False
    )
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    usuarios: Mapped[list[Usuario]] = relationship("Usuario", back_populates="tenant")
    empresas: Mapped[list[Empresa]] = relationship("Empresa", back_populates="tenant")


# ─────────────────────────────────────────────────────────────── Usuário


class Usuario(Base, TimestampMixin):
    """Usuário do escritório."""

    __tablename__ = "usuarios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    senha_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum("admin", "contador", name="role_enum"), default="contador", nullable=False
    )
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="usuarios")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship("RefreshToken", back_populates="usuario")
    permissoes: Mapped[list[Permissao]] = relationship("Permissao", back_populates="usuario")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_usuario_tenant_email"),
    )


class RefreshToken(Base):
    """Refresh tokens — permite revogação sem estado no JWT."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False)
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revogado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    usuario: Mapped[Usuario] = relationship("Usuario", back_populates="refresh_tokens")

    __table_args__ = (Index("ix_refresh_tokens_jti", "jti"),)


# ─────────────────────────────────────────────────────────────── Empresa (Parceiro)


class Empresa(Base, TimestampMixin):
    """Empresa cliente gerenciada pelo escritório."""

    __tablename__ = "empresas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    razao_social: Mapped[str] = mapped_column(String(300), nullable=False)
    cnpj: Mapped[str] = mapped_column(String(18), nullable=False)
    regime_tributario: Mapped[str] = mapped_column(
        Enum("simples_nacional", "lucro_presumido", "lucro_real", name="regime_enum"),
        nullable=False,
    )
    ativa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="empresas")
    agencias: Mapped[list[AgenciaBancaria]] = relationship("AgenciaBancaria", back_populates="empresa")
    regras: Mapped[list[Regra]] = relationship("Regra", back_populates="empresa")
    permissoes: Mapped[list[Permissao]] = relationship("Permissao", back_populates="empresa")
    contas: Mapped[list[PlanoConta]] = relationship("PlanoConta", back_populates="empresa")
    aplicacoes_financeiras: Mapped[list[AplicacaoFinanceira]] = relationship(
        "AplicacaoFinanceira", back_populates="empresa"
    )
    contrapartes: Mapped[list[Contraparte]] = relationship(
        "Contraparte", back_populates="empresa"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "cnpj", name="uq_empresa_tenant_cnpj"),
    )


class Permissao(Base):
    """Acesso de um usuário a uma empresa específica."""

    __tablename__ = "permissoes"

    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), primary_key=True)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    modulos: Mapped[str] = mapped_column(String(500), default="*", nullable=False)
    # "*" = acesso total | "extrato,regras" = módulos específicos

    usuario: Mapped[Usuario] = relationship("Usuario", back_populates="permissoes")
    empresa: Mapped[Empresa] = relationship("Empresa", back_populates="permissoes")


# ─────────────────────────────────────────────────────────────── Plano de Contas


class PlanoConta(Base, TimestampMixin):
    __tablename__ = "plano_contas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    conta_numero: Mapped[int | None] = mapped_column(nullable=True)  # ID numérico MrContador
    codigo: Mapped[str] = mapped_column(String(30), nullable=False)
    descricao: Mapped[str] = mapped_column(String(300), nullable=False)
    tipo: Mapped[str] = mapped_column(String(50), nullable=False)
    tipo_sa: Mapped[str] = mapped_column(String(1), nullable=False, default="A")
    pai_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plano_contas.id"), nullable=True)

    empresa: Mapped[Empresa] = relationship("Empresa", back_populates="contas")
    regras: Mapped[list[Regra]] = relationship("Regra", back_populates="conta")
    pai: Mapped[PlanoConta | None] = relationship(
        "PlanoConta", back_populates="filhos", remote_side="PlanoConta.id"
    )
    filhos: Mapped[list[PlanoConta]] = relationship(
        "PlanoConta", back_populates="pai"
    )

    __table_args__ = (
        # Parcial (só linhas ativas): sem isso, "Excluir Todas" (soft delete)
        # deixa o código soterrado — reimportar o mesmo plano de contas depois
        # de limpar tudo esbarra na conta "excluída" que ainda ocupa o código.
        Index(
            "uq_plano_empresa_codigo_ativo",
            "empresa_id",
            "codigo",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    @property
    def nivel(self) -> int:
        """Profundidade na hierarquia baseada no código (ex: 1.1.2 → nível 3)."""
        return len(self.codigo.split("."))


class PlanoContaFaixaTipo(Base, TimestampMixin):
    """Faixa de código → tipo contábil, configurada por empresa.

    Usada como fallback na importação quando a planilha não tem coluna
    "Tipo": se o código do lançamento cai dentro de uma faixa configurada,
    o tipo é inferido dali. Configuração explícita do usuário, não
    heurística do sistema — evita adivinhar a natureza contábil.
    """

    __tablename__ = "plano_contas_faixas_tipo"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(50), nullable=False)
    codigo_de: Mapped[str] = mapped_column(String(30), nullable=False)
    codigo_ate: Mapped[str] = mapped_column(String(30), nullable=False)

    __table_args__ = (
        Index("ix_plano_contas_faixas_tipo_empresa", "empresa_id"),
    )


# ─────────────────────────────────────────────────────────────── Agência Bancária


class AgenciaBancaria(Base, TimestampMixin):
    __tablename__ = "agencias_bancarias"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    banco_sigla: Mapped[str] = mapped_column(String(20), nullable=False)
    agencia: Mapped[str] = mapped_column(String(10), nullable=False)
    numero: Mapped[str] = mapped_column(String(20), nullable=False)
    digito: Mapped[str | None] = mapped_column(String(5), nullable=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    conta_contabil_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plano_contas.id"), nullable=True, unique=True
    )

    empresa: Mapped[Empresa] = relationship("Empresa", back_populates="agencias")
    regras: Mapped[list[Regra]] = relationship("Regra", back_populates="agencia")
    conta_contabil: Mapped[PlanoConta | None] = relationship("PlanoConta")

    __table_args__ = (
        UniqueConstraint(
            "empresa_id",
            "banco_sigla",
            "agencia",
            "numero",
            name="uq_agencia_empresa_banco_agencia_numero",
        ),
    )

    @property
    def descricao(self) -> str:
        parts = [self.banco_sigla, self.agencia, self.numero]
        if self.digito:
            parts.append(self.digito)
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────── Aplicação Financeira

TIPOS_APLICACAO_FINANCEIRA = ("cdb", "poupanca", "fundo", "tesouro_direto", "lci_lca", "outros")


class AplicacaoFinanceira(Base, TimestampMixin):
    """Aplicação financeira da empresa (CDB, poupança, fundo, tesouro etc.).

    Só o registro/acompanhamento do valor aplicado e do valor atual — sem
    integração com extrato ou conciliação nesta primeira versão.
    """

    __tablename__ = "aplicacoes_financeiras"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    agencia_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agencias_bancarias.id"), nullable=True
    )
    instituicao: Mapped[str] = mapped_column(String(200), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    descricao: Mapped[str | None] = mapped_column(String(300), nullable=True)
    valor_aplicado: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    data_aplicacao: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valor_atual: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    data_atualizacao_valor: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    data_vencimento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observacao: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    empresa: Mapped[Empresa] = relationship("Empresa", back_populates="aplicacoes_financeiras")
    agencia: Mapped[AgenciaBancaria | None] = relationship("AgenciaBancaria")

    @property
    def rendimento(self) -> Decimal | None:
        """Diferença entre o valor atual (última atualização) e o valor aplicado."""
        if self.valor_atual is None:
            return None
        return self.valor_atual - self.valor_aplicado


# ─────────────────────────────────────────────────────────────── Regra


class Regra(Base, TimestampMixin):
    __tablename__ = "regras"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    conta_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plano_contas.id"), nullable=False)
    agencia_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencias_bancarias.id"), nullable=False)
    descricao: Mapped[str] = mapped_column(String(500), nullable=False)
    historico: Mapped[str] = mapped_column(String(500), nullable=False)
    historico_normalizado: Mapped[str] = mapped_column(String(500), nullable=False)
    dc: Mapped[str] = mapped_column(Enum("D", "C", name="dc_enum"), nullable=False)
    tipo: Mapped[str] = mapped_column(
        Enum("automatica", "manual", name="tipo_regra_enum"), nullable=False
    )
    manter_historico: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    empresa: Mapped[Empresa] = relationship("Empresa", back_populates="regras")
    conta: Mapped[PlanoConta] = relationship("PlanoConta", back_populates="regras")
    agencia: Mapped[AgenciaBancaria] = relationship("AgenciaBancaria", back_populates="regras")

    __table_args__ = (
        Index(
            "uq_regra_empresa_agencia_historico_normalizado_ativa",
            "empresa_id",
            "agencia_id",
            "historico_normalizado",
            unique=True,
            postgresql_where=text("ativa = true AND deleted_at IS NULL"),
            sqlite_where=text("ativa = 1 AND deleted_at IS NULL"),
        ),
    )

    @validates("historico")
    def _normalizar_historico(self, _key: str, value: str) -> str:
        self.historico_normalizado = value.strip().lower()
        return value


# ─────────────────────────────────────────────────────────────── Contraparte


class Contraparte(Base, TimestampMixin):
    """Fornecedor ou cliente identificado por CPF/CNPJ, com conta contábil padrão.

    Cadastro separado de `Regra`: o mesmo histórico bancário pode variar por
    agência/redação, mas a identidade fiscal e a conta padrão de um fornecedor
    ou cliente não dependem disso. `tipo` cobre os dois sentidos porque o
    mesmo documento pode aparecer tanto em pagamentos (fornecedor) quanto em
    recebimentos (cliente).
    """

    __tablename__ = "contrapartes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(
        Enum("fornecedor", "cliente", "ambos", name="tipo_contraparte_enum"), nullable=False
    )
    documento: Mapped[str] = mapped_column(String(14), nullable=False)  # CPF/CNPJ, só dígitos
    razao_social: Mapped[str] = mapped_column(String(300), nullable=False)
    nome_fantasia: Mapped[str | None] = mapped_column(String(300), nullable=True)
    conta_contabil_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plano_contas.id"), nullable=False
    )
    origem: Mapped[str] = mapped_column(
        Enum(
            "manual", "nota_fiscal", "comprovante", "historico_extrato", "backfill",
            name="origem_contraparte_enum",
        ),
        default="manual",
        nullable=False,
    )
    confirmado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    empresa: Mapped[Empresa] = relationship("Empresa", back_populates="contrapartes")
    conta_contabil: Mapped[PlanoConta] = relationship("PlanoConta")

    __table_args__ = (
        Index(
            "uq_contraparte_empresa_documento_ativa",
            "empresa_id",
            "documento",
            unique=True,
            postgresql_where=text("ativa = true AND deleted_at IS NULL"),
            sqlite_where=text("ativa = 1 AND deleted_at IS NULL"),
        ),
        Index("ix_contraparte_empresa_razao_social", "empresa_id", "razao_social"),
    )


# ─────────────────────────────────────────────────────────────── Extrato / Transação


class Transacao(Base, TimestampMixin):
    __tablename__ = "transacoes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    agencia_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencias_bancarias.id"), nullable=False)
    data: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2, asdecimal=True), nullable=False)
    historico: Mapped[str] = mapped_column(String(500), nullable=False)
    dc: Mapped[str] = mapped_column(Enum("D", "C", name="dc_transacao_enum"), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("pendente", "processada", "erro", name="status_transacao_enum"),
        default="pendente",
        nullable=False,
    )
    hash_dedup: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 para dedup

    __table_args__ = (
        UniqueConstraint("empresa_id", "hash_dedup", name="uq_transacao_empresa_hash"),
        Index("ix_transacao_empresa_status", "empresa_id", "status"),
    )


# ─────────────────────────────────────────────────────────────── Registro Contábil


class RegistroContabil(Base, TimestampMixin):
    __tablename__ = "registros_contabeis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    transacao_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transacoes.id"), nullable=True
    )
    lancamento_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    conta_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plano_contas.id"), nullable=False)
    agencia_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agencias_bancarias.id"), nullable=False)
    descricao: Mapped[str] = mapped_column(String(500), nullable=False)
    historico: Mapped[str] = mapped_column(String(500), nullable=False)
    historico_extrato: Mapped[str] = mapped_column(String(500), nullable=False)
    dc: Mapped[str] = mapped_column(Enum("D", "C", name="dc_registro_enum"), nullable=False)
    tipo_regra: Mapped[str] = mapped_column(String(50), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2, asdecimal=True), nullable=False)
    data_lancamento: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    transacao: Mapped[Transacao | None] = relationship("Transacao")
    conta: Mapped[PlanoConta] = relationship("PlanoConta")
    agencia: Mapped[AgenciaBancaria] = relationship("AgenciaBancaria")

    __table_args__ = (
        Index("ix_registro_empresa_data", "empresa_id", "data_lancamento"),
        Index("ix_registro_lancamento", "lancamento_id"),
        Index(
            "uq_registro_transacao_dc_ativo",
            "transacao_id",
            "dc",
            unique=True,
            postgresql_where=text("transacao_id IS NOT NULL AND deleted_at IS NULL"),
            sqlite_where=text("transacao_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )


# ─────────────────────────────────────────────────────────────── Nota Fiscal (NF-e / NFS-e)


class NotaFiscal(Base, TimestampMixin):
    """NF-e e NFS-e recebidas/emitidas pela empresa."""

    __tablename__ = "notas_fiscais"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(
        Enum("nfe", "nfse", name="tipo_nota_enum"), nullable=False
    )
    numero: Mapped[str] = mapped_column(String(50), nullable=False)
    serie: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cnpj_emitente: Mapped[str] = mapped_column(String(18), nullable=False)
    nome_emitente: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cnpj_destinatario: Mapped[str | None] = mapped_column(String(18), nullable=True)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2, asdecimal=True), nullable=False)
    data_emissao: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("pendente", "associada", "cancelada", name="status_nota_enum"),
        default="pendente",
        nullable=False,
    )
    transacao_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transacoes.id"), nullable=True
    )
    chave_acesso: Mapped[str | None] = mapped_column(String(44), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    observacao: Mapped[str | None] = mapped_column(String(500), nullable=True)
    origem: Mapped[str] = mapped_column(
        Enum("xml_assinado", "ocr", name="origem_nota_enum"),
        default="xml_assinado",
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("empresa_id", "chave_acesso", name="uq_nota_empresa_chave"),
        UniqueConstraint("empresa_id", "dedup_key", name="uq_nota_empresa_dedup"),
        Index("ix_nota_empresa_status", "empresa_id", "status"),
        Index("ix_nota_empresa_emissao", "empresa_id", "data_emissao"),
    )


# ─────────────────────────────────────────────────────────────── Comprovante de Pagamento


class Comprovante(Base, TimestampMixin):
    """Comprovante de pagamento associado a uma transação bancária."""

    __tablename__ = "comprovantes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    agencia_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agencias_bancarias.id"), nullable=True)
    transacao_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transacoes.id"), nullable=True)
    favorecido: Mapped[str | None] = mapped_column(String(300), nullable=True)
    cpf_cnpj: Mapped[str | None] = mapped_column(String(18), nullable=True)
    data_pagamento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_vencimento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valor_documento: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2, asdecimal=True), nullable=True
    )
    valor_pago: Mapped[Decimal] = mapped_column(
        Numeric(15, 2, asdecimal=True), nullable=False
    )
    juros: Mapped[Decimal] = mapped_column(
        Numeric(15, 2, asdecimal=True), default=Decimal("0.00"), nullable=False
    )
    multa: Mapped[Decimal] = mapped_column(
        Numeric(15, 2, asdecimal=True), default=Decimal("0.00"), nullable=False
    )
    desconto: Mapped[Decimal] = mapped_column(
        Numeric(15, 2, asdecimal=True), default=Decimal("0.00"), nullable=False
    )
    observacao: Mapped[str | None] = mapped_column(String(500), nullable=True)
    arquivo_nome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    arquivo_base64: Mapped[str | None] = mapped_column(Text, nullable=True)  # PDF/imagem em base64

    __table_args__ = (
        Index("ix_comprovante_empresa", "empresa_id"),
        Index("ix_comprovante_transacao", "transacao_id"),
    )


# ─────────────────────────────────────────────────────────────── Cartão de Crédito


class CartaoCredito(Base, TimestampMixin):
    """Cartão de crédito corporativo cadastrado pela empresa."""

    __tablename__ = "cartoes_credito"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    bandeira: Mapped[str] = mapped_column(String(20), nullable=False)  # visa/master/elo/amex/hipercard/outros
    ultimos_digitos: Mapped[str | None] = mapped_column(String(4), nullable=True)
    dia_fechamento: Mapped[int] = mapped_column(Integer, nullable=False)   # 1-28
    dia_vencimento: Mapped[int] = mapped_column(Integer, nullable=False)   # 1-28
    limite: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2, asdecimal=True), nullable=True
    )
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    faturas: Mapped[list[FaturaCartao]] = relationship("FaturaCartao", back_populates="cartao")

    __table_args__ = (Index("ix_cartao_empresa", "empresa_id"),)


class FaturaCartao(Base, TimestampMixin):
    """Fatura mensal de um cartão de crédito."""

    __tablename__ = "faturas_cartao"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    cartao_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cartoes_credito.id"), nullable=False)
    competencia: Mapped[str] = mapped_column(String(7), nullable=False)   # "2024-01"
    data_fechamento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_vencimento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valor_total: Mapped[Decimal] = mapped_column(
        Numeric(15, 2, asdecimal=True), default=Decimal("0.00"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Enum("aberta", "fechada", "paga", name="status_fatura_enum"),
        nullable=False,
        default="aberta",
    )
    # Transação bancária que pagou a fatura
    transacao_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transacoes.id"), nullable=True)
    observacao: Mapped[str | None] = mapped_column(String(500), nullable=True)

    cartao: Mapped[CartaoCredito] = relationship("CartaoCredito", back_populates="faturas")
    lancamentos: Mapped[list[LancamentoCartao]] = relationship("LancamentoCartao", back_populates="fatura")

    __table_args__ = (
        UniqueConstraint("cartao_id", "competencia", name="uq_fatura_cartao_competencia"),
        UniqueConstraint("transacao_id", name="uq_fatura_transacao"),
        Index("ix_fatura_empresa", "empresa_id"),
        Index("ix_fatura_status", "empresa_id", "status"),
    )


class LancamentoCartao(Base, TimestampMixin):
    """Lançamento individual (gasto) em uma fatura de cartão."""

    __tablename__ = "lancamentos_cartao"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    fatura_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faturas_cartao.id"), nullable=False)
    data_compra: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    descricao: Mapped[str] = mapped_column(String(500), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2, asdecimal=True), nullable=False)
    # Conta contábil opcional (para classificação)
    conta_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plano_contas.id"), nullable=True)
    parcela_atual: Mapped[int | None] = mapped_column(Integer, nullable=True)   # ex: 1
    parcela_total: Mapped[int | None] = mapped_column(Integer, nullable=True)   # ex: 3

    fatura: Mapped[FaturaCartao] = relationship("FaturaCartao", back_populates="lancamentos")

    __table_args__ = (Index("ix_lancamento_fatura", "fatura_id"),)


# ─────────────────────────────────────────────────────────────── Open Banking


class ConexaoBancaria(Base, TimestampMixin):
    """Conexão com um banco via Open Banking (Pluggy ou mock)."""

    __tablename__ = "conexoes_bancarias"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    # Agência criada/vinculada na primeira sincronização
    agencia_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agencias_bancarias.id"), nullable=True)
    provedor: Mapped[str] = mapped_column(String(20), nullable=False, default="mock")  # pluggy | mock
    # IDs externos do provedor
    item_id: Mapped[str] = mapped_column(String(100), nullable=False)
    account_id_externo: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Informações da instituição
    instituicao_nome: Mapped[str] = mapped_column(String(200), nullable=False)
    instituicao_codigo: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISPB/COMPE
    banco_sigla: Mapped[str] = mapped_column(String(20), nullable=False)
    agencia_numero: Mapped[str | None] = mapped_column(String(20), nullable=True)
    conta_numero: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Estado da conexão
    status: Mapped[str] = mapped_column(
        Enum("pendente", "ativa", "expirada", "erro", name="status_conexao_enum"),
        nullable=False,
        default="pendente",
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_transacoes_sync: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    erro_msg: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index(
            "uq_conexao_empresa_provedor_conta",
            "empresa_id",
            "provedor",
            "account_id_externo",
            unique=True,
            postgresql_where=text(
                "deleted_at IS NULL AND account_id_externo IS NOT NULL"
            ),
            sqlite_where=text(
                "deleted_at IS NULL AND account_id_externo IS NOT NULL"
            ),
        ),
        Index("ix_conexao_empresa", "empresa_id"),
        Index("ix_conexao_status", "empresa_id", "status"),
    )


# ─────────────────────────────────────────────────────────────── NEO — Log de Decisão


class NeoDecisao(Base):
    """Rastreia por que cada transação foi ou não associada a uma regra pelo NEO."""

    __tablename__ = "neo_decisoes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    transacao_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transacoes.id"), nullable=False)
    regra_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("regras.id"), nullable=True)
    conta_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plano_contas.id"), nullable=True
    )
    resultado: Mapped[str] = mapped_column(
        Enum("associada", "sem_regra", "erro", name="resultado_neo_enum"), nullable=False
    )
    estrategia: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        # ex: "exato", "substring", "manual"
    )
    motivo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    transacao: Mapped[Transacao] = relationship("Transacao")
    regra: Mapped[Regra | None] = relationship("Regra")
    conta: Mapped[PlanoConta | None] = relationship("PlanoConta")

    __table_args__ = (
        Index("ix_neo_empresa_resultado", "empresa_id", "resultado"),
        Index("ix_neo_transacao", "transacao_id"),
        Index(
            "uq_neo_sem_regra_transacao",
            "transacao_id",
            unique=True,
            postgresql_where=text("resultado = 'sem_regra'"),
            sqlite_where=text("resultado = 'sem_regra'"),
        ),
    )


# ─────────────────────────────────────────────────────────────── Job de Exportação


class ExportJob(Base):
    """Controla jobs de exportação assíncrona para ERP."""

    __tablename__ = "export_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    formato: Mapped[str] = mapped_column(
        Enum("csv", "xlsx", "txt", name="formato_export_enum"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Enum("pendente", "processando", "concluido", "erro", name="status_job_enum"),
        default="pendente",
        nullable=False,
    )
    filtro_de: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filtro_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_registros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arquivo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    erro_msg: Mapped[str | None] = mapped_column(String(500), nullable=True)
    criado_por: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_export_job_empresa", "empresa_id", "status"),)


# ─────────────────────────────────────────────────────────────── Auditoria


class AuditLog(Base):
    """Registro imutável de toda ação de mutação."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    empresa_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    acao: Mapped[str] = mapped_column(String(100), nullable=False)  # ex: "regra.criada"
    entidade: Mapped[str] = mapped_column(String(100), nullable=False)  # ex: "regra"
    entidade_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    dados_antes: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    dados_depois: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (Index("ix_audit_tenant_acao", "tenant_id", "acao"),)


# ──────────────────────────────────────────────────────── CONCILPRO
# Modelos do módulo de conciliação de fornecedores (Razão de Contas a Pagar).
# Usam Integer PKs (herança do conBank) e coluna created_at simples.
# Prefixo cp_ nas tabelas para isolar do restante do schema.

from sqlalchemy import Column as _Col, Integer as _Int, String as _Str, Numeric as _Num
from sqlalchemy import Boolean as _Bool, DateTime as _DT, Date as _Date, Text as _Txt
from sqlalchemy import ForeignKey as _FK
from sqlalchemy.orm import relationship as _rel

# Timestamps sem timezone — compatível com TIMESTAMP WITHOUT TIME ZONE + asyncpg
def _utcnow():
    """Retorna UTC naive (sem tzinfo). Necessário para colunas TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(UTC).replace(tzinfo=None)


class CpArquivo(Base):
    """CONCILPRO — arquivo PDF importado (Razão de Fornecedores)."""
    __tablename__ = "cp_arquivo"

    id              = _Col(_Int, primary_key=True, index=True)
    empresa_id      = _Col(UUID(as_uuid=True), _FK("empresas.id"), nullable=False)
    nome_arquivo    = _Col(_Str(255), nullable=False)
    hash_arquivo    = _Col(_Str(64), nullable=False)
    empresa         = _Col(_Str(255))
    cnpj_empresa    = _Col(_Str(18))
    total_fornecedores = _Col(_Int, default=0)
    total_lancamentos  = _Col(_Int, default=0)
    data_inicio     = _Col(_Date)
    data_fim        = _Col(_Date)
    status          = _Col(_Str(20), default="PROCESSANDO")  # PROCESSANDO | CONCLUIDO | ERRO
    mensagem_erro   = _Col(_Txt)
    created_at      = _Col(_DT, default=_utcnow)

    fornecedores    = _rel("CpFornecedor", back_populates="arquivo_origem")

    __table_args__ = (
        UniqueConstraint("empresa_id", "hash_arquivo", name="uq_cp_arquivo_empresa_hash"),
        Index("ix_cp_arquivo_empresa", "empresa_id"),
    )


class CpFornecedor(Base):
    """CONCILPRO — conta de fornecedor extraída do Razão."""
    __tablename__ = "cp_fornecedor"

    id                  = _Col(_Int, primary_key=True, index=True)
    empresa_id          = _Col(UUID(as_uuid=True), _FK("empresas.id"), nullable=False)
    arquivo_origem_id   = _Col(_Int, _FK("cp_arquivo.id"))
    codigo_conta        = _Col(_Str(10), nullable=False)
    conta_contabil      = _Col(_Str(50), nullable=False)
    nome_fornecedor     = _Col(_Txt, nullable=False)
    cnpj                = _Col(_Str(18))
    saldo_anterior      = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    saldo_anterior_tipo = _Col(_Str(1))
    total_debito        = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    total_credito       = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    saldo_final         = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    saldo_final_tipo    = _Col(_Str(1))
    status_pagamento    = _Col(_Str(20))   # QUITADO | EM_ABERTO | ADIANTADO | SEM_MOVIMENTO
    valor_a_pagar       = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    qtd_nfs_pendentes   = _Col(_Int, default=0)
    qtd_nfs_parciais    = _Col(_Int, default=0)
    divergencia_calculo = _Col(_Bool, default=False)
    mensagem_erro       = _Col(_Txt)
    created_at          = _Col(_DT, default=_utcnow)
    updated_at          = _Col(_DT, default=_utcnow, onupdate=_utcnow)

    arquivo_origem  = _rel("CpArquivo", back_populates="fornecedores")
    lancamentos     = _rel("CpLancamento", back_populates="fornecedor", cascade="all, delete-orphan")
    conciliacoes    = _rel("CpConciliacao", back_populates="fornecedor", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_cp_fornecedor_empresa", "empresa_id"),
        Index("ix_cp_fornecedor_conta", "codigo_conta", "conta_contabil"),
        Index("ix_cp_fornecedor_status", "status_pagamento"),
    )


class CpLancamento(Base):
    """CONCILPRO — lançamento individual no Razão do fornecedor."""
    __tablename__ = "cp_lancamento"

    id                    = _Col(_Int, primary_key=True, index=True)
    empresa_id            = _Col(UUID(as_uuid=True), _FK("empresas.id"), nullable=False)
    fornecedor_id         = _Col(_Int, _FK("cp_fornecedor.id"), nullable=False)
    data_lancamento       = _Col(_Date, nullable=False)
    lote                  = _Col(_Str(50))
    historico             = _Col(_Txt, nullable=False)
    conta_partida         = _Col(_Str(20))
    valor_debito          = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    valor_credito         = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    saldo_apos_lancamento = _Col(_Num(15, 2, asdecimal=True))
    saldo_tipo            = _Col(_Str(1))
    tipo_operacao         = _Col(_Str(20))   # COMPRA | PAGAMENTO | DEVOLUCAO
    numero_nf             = _Col(_Str(50))
    cnpj_historico        = _Col(_Str(18))
    valor_pago_parcial    = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    valor_saldo           = _Col(_Num(15, 2, asdecimal=True), default=Decimal("0.00"))
    status_pagamento      = _Col(_Str(20))   # PAGO | PARCIAL | PENDENTE
    classificado_por_ia   = _Col(_Bool, default=False)
    created_at            = _Col(_DT, default=_utcnow)

    fornecedor           = _rel("CpFornecedor", back_populates="lancamentos")
    conciliacoes_credito = _rel("CpConciliacao", foreign_keys="CpConciliacao.lancamento_credito_id",
                                back_populates="lancamento_credito")
    conciliacoes_debito  = _rel("CpConciliacao", foreign_keys="CpConciliacao.lancamento_debito_id",
                                back_populates="lancamento_debito")

    __table_args__ = (
        Index("ix_cp_lancamento_empresa", "empresa_id"),
        Index("ix_cp_lancamento_forn_data", "fornecedor_id", "data_lancamento"),
        Index("ix_cp_lancamento_tipo", "tipo_operacao"),
        Index("ix_cp_lancamento_status", "status_pagamento"),
        Index("ix_cp_lancamento_nf", "numero_nf"),
    )


class CpConciliacao(Base):
    """CONCILPRO — vínculo FIFO entre compra (crédito) e pagamento (débito)."""
    __tablename__ = "cp_conciliacao"

    id                    = _Col(_Int, primary_key=True, index=True)
    empresa_id            = _Col(UUID(as_uuid=True), _FK("empresas.id"), nullable=False)
    fornecedor_id         = _Col(_Int, _FK("cp_fornecedor.id"), nullable=False)
    lancamento_credito_id = _Col(_Int, _FK("cp_lancamento.id"))
    lancamento_debito_id  = _Col(_Int, _FK("cp_lancamento.id"))
    valor_conciliado      = _Col(_Num(15, 2, asdecimal=True), nullable=False)
    metodo_match          = _Col(_Str(20))   # AUTO_NF | AUTO_VALOR_EXATO | AUTO_FIFO | MANUAL
    confianca             = _Col(_Int)       # 0-100
    observacao            = _Col(_Txt)
    created_at            = _Col(_DT, default=_utcnow)

    fornecedor         = _rel("CpFornecedor", back_populates="conciliacoes")
    lancamento_credito = _rel("CpLancamento", foreign_keys=[lancamento_credito_id],
                              back_populates="conciliacoes_credito")
    lancamento_debito  = _rel("CpLancamento", foreign_keys=[lancamento_debito_id],
                              back_populates="conciliacoes_debito")

    __table_args__ = (
        Index("ix_cp_conciliacao_empresa", "empresa_id"),
        Index("ix_cp_conciliacao_forn", "fornecedor_id"),
    )


class CpDivergencia(Base):
    """CONCILPRO — divergência contábil detectada na conciliação."""
    __tablename__ = "cp_divergencia"

    id               = _Col(_Int, primary_key=True, index=True)
    empresa_id       = _Col(UUID(as_uuid=True), _FK("empresas.id"), nullable=False)
    fornecedor_id    = _Col(_Int, _FK("cp_fornecedor.id"))
    lancamento_id    = _Col(_Int, _FK("cp_lancamento.id"))
    tipo             = _Col(_Str(50), nullable=False)
    severidade       = _Col(_Str(20))   # CRITICA | ALTA | MEDIA | BAIXA
    descricao        = _Col(_Txt, nullable=False)
    valor_esperado   = _Col(_Num(15, 2, asdecimal=True))
    valor_encontrado = _Col(_Num(15, 2, asdecimal=True))
    diferenca        = _Col(_Num(15, 2, asdecimal=True))
    resolvido        = _Col(_Bool, default=False)
    observacao_resolucao = _Col(_Txt)
    created_at       = _Col(_DT, default=_utcnow)

    __table_args__ = (
        Index("ix_cp_divergencia_empresa", "empresa_id"),
        Index("ix_cp_divergencia_forn", "fornecedor_id"),
        Index("ix_cp_divergencia_resolvido", "resolvido"),
    )
