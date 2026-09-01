"""Schemas Pydantic para Notas Fiscais (NF-e / NFS-e)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.schemas.types import CNPJ


class NotaFiscalCreate(BaseModel):
    tipo: str = Field(..., description="nfe ou nfse")
    numero: str = Field(..., min_length=1, max_length=50)
    serie: str | None = Field(default=None, max_length=10)
    cnpj_emitente: CNPJ = Field(..., description="CNPJ do emitente")
    nome_emitente: str | None = Field(default=None, max_length=300)
    cnpj_destinatario: CNPJ | None = None
    valor: Decimal = Field(..., gt=0)
    data_emissao: datetime
    chave_acesso: str | None = Field(default=None, max_length=60)
    observacao: str | None = Field(default=None, max_length=500)
    origem: str = Field(
        default="xml_assinado",
        description=(
            "Procedência, da mais forte para a mais fraca: xml_assinado "
            "(assinatura digital conferida), xml_nao_verificado (XML autorizado "
            "pela SEFAZ cuja assinatura não fecha) ou ocr (lido de PDF/imagem)"
        ),
    )
    # Por que a assinatura não conferiu, quando não conferiu. Vai junto para a
    # decisão de importar mesmo assim ficar auditável na tela e na exportação.
    assinatura_motivo: str | None = Field(default=None, max_length=200)

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("nfe", "nfse"):
            raise ValueError("tipo deve ser 'nfe' ou 'nfse'.")
        return v

    @field_validator("chave_acesso", mode="before")
    @classmethod
    def valida_chave_acesso(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = "".join(c for c in str(v) if c.isdigit())
        if len(digits) != 44:
            raise ValueError("Chave de acesso deve ter 44 dígitos.")
        return digits

    @field_validator("origem")
    @classmethod
    def valida_origem(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("xml_assinado", "xml_nao_verificado", "ocr"):
            raise ValueError(
                "origem deve ser 'xml_assinado', 'xml_nao_verificado' ou 'ocr'."
            )
        return v


class AssociarTransacaoRequest(BaseModel):
    transacao_id: UUID


class NotaFiscalResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    tipo: str
    numero: str
    serie: str | None
    cnpj_emitente: str
    nome_emitente: str | None
    valor: Decimal
    data_emissao: datetime
    status: str
    transacao_id: UUID | None
    transacao_descricao: str | None = None
    transacao_valor: Decimal | None = None
    transacao_dc: str | None = None
    chave_acesso: str | None
    observacao: str | None
    origem: str
    assinatura_motivo: str | None = None

    model_config = {"from_attributes": True}


class NotaFiscalListResponse(BaseModel):
    items: list[NotaFiscalResponse]
    total: int
    page: int
    page_size: int


class ImportXmlResponse(BaseModel):
    importadas: int
    duplicadas: int
    erros: list[str]
    message: str
