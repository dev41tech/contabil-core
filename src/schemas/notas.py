"""Schemas Pydantic para Notas Fiscais (NF-e / NFS-e)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NotaFiscalCreate(BaseModel):
    tipo: str = Field(..., description="nfe ou nfse")
    numero: str = Field(..., min_length=1, max_length=50)
    serie: str | None = Field(default=None, max_length=10)
    cnpj_emitente: str = Field(..., description="CNPJ do emitente")
    nome_emitente: str | None = Field(default=None, max_length=300)
    cnpj_destinatario: str | None = None
    valor: float = Field(..., gt=0)
    data_emissao: datetime
    chave_acesso: str | None = Field(default=None, max_length=60)
    observacao: str | None = Field(default=None, max_length=500)

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("nfe", "nfse"):
            raise ValueError("tipo deve ser 'nfe' ou 'nfse'.")
        return v

    @field_validator("cnpj_emitente", "cnpj_destinatario", mode="before")
    @classmethod
    def normaliza_cnpj(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos.")
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"

    @field_validator("chave_acesso", mode="before")
    @classmethod
    def valida_chave_acesso(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = "".join(c for c in str(v) if c.isdigit())
        if len(digits) != 44:
            raise ValueError("Chave de acesso deve ter 44 dígitos.")
        return digits


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
    valor: float
    data_emissao: datetime
    status: str
    transacao_id: UUID | None
    chave_acesso: str | None
    observacao: str | None

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
