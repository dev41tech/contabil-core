"""Schemas Pydantic para o NEO (motor de matching)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from src.schemas.types import Competencia


class NeoProcessarRequest(BaseModel):
    agencia_id: UUID | None = None  # None = processar todas as agências da empresa
    mes: Competencia | None = Field(
        default=None,
        description="Filtra transações do mês (AAAA-MM). None = processa todas as pendentes.",
    )


class NeoResultado(BaseModel):
    empresa_id: UUID
    total_pendentes: int
    associadas: int
    sem_regra: int
    erros: int
    # Auto-associações (tasks 5 & 6)
    comprovantes_associados: int = 0
    notas_associadas: int = 0
    # Classificadas sem regra, só pelo cadastro de contrapartes (itens 1+2 do
    # PDF de feedback dos contadores) — subconjunto de `associadas`.
    classificadas_por_contraparte: int = 0
    processado_em: datetime


class NeoDecisaoResponse(BaseModel):
    id: UUID
    transacao_id: UUID
    transacao_descricao: str | None = None
    # Campos extras para popular os modais de ação manual
    transacao_valor: Decimal | None = None
    transacao_dc: str | None = None
    agencia_id: UUID | None = None
    regra_id: UUID | None
    regra_descricao: str | None = None
    resultado: str       # associada | sem_regra | erro
    estrategia: str | None
    motivo: str | None
    processado_em: datetime

    model_config = {"from_attributes": True}


class NeoDecisaoListResponse(BaseModel):
    items: list[NeoDecisaoResponse]
    total: int
    page: int
    page_size: int


class NeoAssociarManualRequest(BaseModel):
    conta_id: UUID
    descricao: str = Field(..., min_length=2, max_length=500)
