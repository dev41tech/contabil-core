"""Schemas Pydantic para Extrato Bancário."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class TransacaoResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    agencia_id: UUID
    # Serializa como "2026-07-01", sem hora e sem "Z". Enquanto era datetime, o
    # front recebia meia-noite UTC e renderizava o dia anterior em Brasília.
    data: date
    valor: Decimal
    # Saldo da conta após o lançamento, como impresso no extrato. `null` quando a
    # origem não informa (importação por OFX) — a tela deve exibir vazio, não zero.
    saldo_apos: Decimal | None = None
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
    # Linhas recusadas porque o valor lido não confere com a linha do extrato —
    # ver `src.domain.extrato.validacao`. Contadas à parte de `erros` porque a
    # causa e a ação do contador são outras: aqui o arquivo foi lido, mas um
    # número específico não é confiável.
    rejeitadas: int = 0
    motivos_rejeicao: list[str] = []
    transacoes: list[TransacaoResponse]


class ExtratoPendentesResponse(BaseModel):
    items: list[TransacaoResponse]
    total: int
    page: int
    page_size: int


class TransacaoFiltro(BaseModel):
    status: str | None = None          # pendente | processada | erro
    agencia_id: UUID | None = None
    # Busca parcial no histórico do banco, sem diferenciar maiúsculas.
    historico: str | None = None
    dc: str | None = None              # D | C
    # `Transacao.valor` é sempre positivo — o sinal mora em `dc`. A faixa,
    # portanto, é sobre o módulo do lançamento.
    valor_min: Decimal | None = None
    valor_max: Decimal | None = None
    # Datas de calendário, inclusivas nas duas pontas. Como `datetime`,
    # `data_ate=2026-01-31` virava 00:00 e descartava o último dia inteiro.
    data_de: date | None = None
    data_ate: date | None = None
