"""Schemas Pydantic para Comprovantes de Pagamento."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ComprovanteCreate(BaseModel):
    favorecido: str | None = None
    cpf_cnpj: str | None = None
    data_pagamento: datetime | None = None
    data_vencimento: datetime | None = None
    valor_documento: float | None = None
    valor_pago: float = Field(..., gt=0)
    juros: float = Field(default=0, ge=0)
    multa: float = Field(default=0, ge=0)
    desconto: float = Field(default=0, ge=0)
    observacao: str | None = None
    agencia_id: UUID | None = None
    transacao_id: UUID | None = None
    arquivo_nome: str | None = None
    arquivo_base64: str | None = None  # base64 do arquivo


class ComprovanteResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    agencia_id: UUID | None
    transacao_id: UUID | None
    favorecido: str | None
    cpf_cnpj: str | None
    data_pagamento: datetime | None
    data_vencimento: datetime | None
    valor_documento: float | None
    valor_pago: float
    juros: float
    multa: float
    desconto: float
    observacao: str | None
    arquivo_nome: str | None
    tem_arquivo: bool = False

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_custom(cls, obj) -> "ComprovanteResponse":
        data = cls.model_validate(obj)
        data.tem_arquivo = bool(obj.arquivo_base64)
        return data


class ComprovanteListResponse(BaseModel):
    items: list[ComprovanteResponse]
    total: int
    page: int
    page_size: int


class AssociarTransacaoRequest(BaseModel):
    transacao_id: UUID
