"""Schemas para relatórios financeiros: DRE, Balancete e Livro Caixa."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────── DRE


class DRELinha(BaseModel):
    """Uma linha do DRE — conta + valor agregado."""

    conta_id: str
    codigo: str
    descricao: str
    tipo: str
    debitos: float
    creditos: float
    saldo: float  # positivo = favor da conta, negativo = contra


class DREGrupo(BaseModel):
    """Grupo do DRE agrupado por tipo contábil."""

    tipo: str  # receita | custo | despesa | ativo | passivo | patrimonio_liquido
    label: str
    linhas: list[DRELinha]
    total: float


class DREResponse(BaseModel):
    empresa_id: str
    data_de: datetime | None
    data_ate: datetime | None
    grupos: list[DREGrupo]
    # Resultado Líquido = Receitas − Custos − Despesas
    total_receitas: float
    total_custos: float
    total_despesas: float
    resultado_liquido: float


# ─────────────────────────────────────────────────────────────── Balancete


class BalanceteLinha(BaseModel):
    """Uma conta no balancete de verificação."""

    conta_id: str
    codigo: str
    descricao: str
    tipo: str
    nivel: int
    debitos: float
    creditos: float
    saldo_devedor: float   # positivo somente quando D > C
    saldo_credor: float    # positivo somente quando C > D


class BalanceteResponse(BaseModel):
    empresa_id: str
    data_de: datetime | None
    data_ate: datetime | None
    linhas: list[BalanceteLinha]
    total_debitos: float
    total_creditos: float
    total_saldo_devedor: float
    total_saldo_credor: float


# ─────────────────────────────────────────────────────────────── Livro Caixa


class LivroCaixaLancamento(BaseModel):
    """Um lançamento no livro caixa."""

    data: datetime
    historico: str
    dc: str
    valor: float
    saldo_acumulado: float


class LivroCaixaAgencia(BaseModel):
    """Movimentação de uma agência no período."""

    agencia_id: str
    descricao: str
    saldo_inicial: float
    lancamentos: list[LivroCaixaLancamento]
    saldo_final: float
    total_debitos: float
    total_creditos: float


class LivroCaixaResponse(BaseModel):
    empresa_id: str
    data_de: datetime | None
    data_ate: datetime | None
    agencias: list[LivroCaixaAgencia]


# ─────────────────────────────────────────────────────────────── Query params


class RelatorioParams(BaseModel):
    data_de: datetime | None = Field(default=None)
    data_ate: datetime | None = Field(default=None)
