"""Schemas da carteira operacional do escritório."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class CarteiraEmpresaResponse(BaseModel):
    empresa_id: UUID
    razao_social: str
    transacoes_importadas: int
    pendentes: int
    classificadas: int
    erros: int
    ha_extrato_importado: bool
    valor_total_pendente: Decimal


class CarteiraResponse(BaseModel):
    mes: str
    items: list[CarteiraEmpresaResponse]
