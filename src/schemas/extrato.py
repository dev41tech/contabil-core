"""Schemas Pydantic para Extrato Bancário."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TransacaoResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    agencia_id: UUID
    data: datetime
    valor: float
    historico: str
    dc: str
    status: str

    model_config = {"from_attributes": True}


class ImportacaoResult(BaseModel):
    """Resultado de uma importação de extrato OFX."""
    agencia_id: UUID
    total_no_arquivo: int
    importadas: int
    duplicadas: int
    erros: int
    transacoes: list[TransacaoResponse]


class ExtratoPendentesResponse(BaseModel):
    items: list[TransacaoResponse]
    total: int
    page: int
    page_size: int


class TransacaoFiltro(BaseModel):
    status: str | None = None          # pendente | processada | erro
    agencia_id: UUID | None = None
    data_de: datetime | None = None
    data_ate: datetime | None = None
