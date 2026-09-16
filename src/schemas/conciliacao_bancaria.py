"""Relatório da conciliação razão × extrato. Nada disto é gravado."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class LinhaRazao(BaseModel):
    data: date
    valor: Decimal
    historico: str
    lote: str
    contrapartida: str


class LinhaExtrato(BaseModel):
    transacao_id: UUID
    data: date
    valor: Decimal
    historico: str


class GrupoConciliacao(BaseModel):
    tipo: str
    razao: list[LinhaRazao]
    extrato: list[LinhaExtrato]
    diferenca: Decimal


class ResumoConciliacao(BaseModel):
    empresa: str
    conta_razao: str
    conta_bancaria: str
    periodo_inicio: date | None
    periodo_fim: date | None
    lancamentos_razao: int
    lancamentos_extrato: int
    conciliados: int
    pendencias: int
    movimento_razao: Decimal
    movimento_extrato: Decimal
    diferenca: Decimal
    # A soma das pendências explica a diferença inteira? Se não, há algo que o
    # cruzamento não enxergou — e o relatório não deve parecer completo.
    diferenca_explicada: bool
    abertura: list[LinhaRazao]


class RelatorioConciliacao(BaseModel):
    resumo: ResumoConciliacao
    avisos: list[str]
    conciliados_por_tipo: dict[str, int]
    pendencias: list[GrupoConciliacao]
