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


class LinhaSispag(BaseModel):
    """Pagamento da consulta do SISPAG — aqui, o que o banco pagou e o razão não tem."""

    data: date
    valor: Decimal
    tipo: str
    favorecido: str
    documento: str


class GrupoConciliacao(BaseModel):
    tipo: str
    razao: list[LinhaRazao]
    extrato: list[LinhaExtrato]
    diferenca: Decimal
    sispag_faltando: list[LinhaSispag] = []


class ResumoDia(BaseModel):
    data: date
    lancamentos_razao: int
    lancamentos_extrato: int
    movimento_razao: Decimal
    movimento_extrato: Decimal
    diferenca: Decimal
    pendencias: int
    # Casado com um lançamento de outro dia: deixa diferença no dia sem ser erro.
    data_diferente: int
    aplicacao_sem_extrato: int


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
    # Lançamentos de aplicação automática que o extrato não traz — não
    # conferidos, fora das pendências. Ver `cruzamento.py`.
    aplicacao_sem_extrato: int = 0


class RelatorioConciliacao(BaseModel):
    resumo: ResumoConciliacao
    avisos: list[str]
    conciliados_por_tipo: dict[str, int]
    pendencias: list[GrupoConciliacao]
    aplicacao_sem_extrato: list[LinhaRazao] = []
    por_dia: list[ResumoDia] = []
    sispag_usado: bool = False
