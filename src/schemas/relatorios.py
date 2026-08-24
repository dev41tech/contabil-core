"""Schemas para relatórios financeiros: DRE, Balancete e Livro Caixa."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────── DRE


class DRELinha(BaseModel):
    """Uma linha do DRE — conta + valor agregado."""

    conta_id: str
    codigo: str
    descricao: str
    tipo: str
    debitos: Decimal
    creditos: Decimal
    saldo: Decimal  # positivo = favor da conta, negativo = contra


class DREGrupo(BaseModel):
    """Grupo do DRE agrupado por tipo contábil."""

    tipo: str  # receita | custo | despesa | ativo | passivo | patrimonio_liquido
    label: str
    linhas: list[DRELinha]
    total: Decimal


class DREResponse(BaseModel):
    empresa_id: str
    data_de: datetime | None
    data_ate: datetime | None
    grupos: list[DREGrupo]
    # Resultado Líquido = Receitas − Custos − Despesas
    total_receitas: Decimal
    total_custos: Decimal
    total_despesas: Decimal
    resultado_liquido: Decimal


# ─────────────────────────────────────────────────────────────── Balancete


class BalanceteLinha(BaseModel):
    """Uma conta no balancete de verificação."""

    conta_id: str
    codigo: str
    descricao: str
    tipo: str
    nivel: int
    debitos: Decimal
    creditos: Decimal
    saldo_devedor: Decimal   # positivo somente quando D > C
    saldo_credor: Decimal    # positivo somente quando C > D


class BalanceteResponse(BaseModel):
    empresa_id: str
    data_de: datetime | None
    data_ate: datetime | None
    linhas: list[BalanceteLinha]
    total_debitos: Decimal
    total_creditos: Decimal
    total_saldo_devedor: Decimal
    total_saldo_credor: Decimal


# ─────────────────────────────────────────────────────────────── Livro Caixa


class LivroCaixaLancamento(BaseModel):
    """Um lançamento no livro caixa."""

    # Data de calendário: vem de `Transacao.data`, que não guarda hora.
    data: date
    historico: str
    dc: str
    valor: Decimal
    saldo_acumulado: Decimal


class LivroCaixaAgencia(BaseModel):
    """Movimentação de uma agência no período."""

    agencia_id: str
    descricao: str
    saldo_inicial: Decimal
    lancamentos: list[LivroCaixaLancamento]
    saldo_final: Decimal
    total_debitos: Decimal
    total_creditos: Decimal


class LivroCaixaResponse(BaseModel):
    empresa_id: str
    data_de: date | None
    data_ate: date | None
    agencias: list[LivroCaixaAgencia]


# ─────────────────────────────────────────────────────────────── Query params


class RelatorioParams(BaseModel):
    data_de: datetime | None = Field(default=None)
    data_ate: datetime | None = Field(default=None)
