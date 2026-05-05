"""Schemas Pydantic para estatísticas do Dashboard."""

from __future__ import annotations

from pydantic import BaseModel


class ResumoStats(BaseModel):
    total_transacoes: int
    total_conciliados: int
    total_nao_conciliados: int
    total_registros: int
    total_notas: int
    total_comprovantes: int
    percentual_conciliacao: float  # 0-100


class MesStats(BaseModel):
    mes: str  # "2024-01"
    transacoes: int
    registros: int
    comprovantes: int
    notas: int


class AgenciaStats(BaseModel):
    agencia_id: str
    descricao: str  # "Banco Sigla – Agência – Número"
    conciliados: int
    nao_conciliados: int


class StatsResponse(BaseModel):
    resumo: ResumoStats
    mensal: list[MesStats]
    por_agencia: list[AgenciaStats]
