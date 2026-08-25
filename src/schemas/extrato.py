"""Schemas Pydantic para Extrato Bancário."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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


class ImportacaoResponse(BaseModel):
    id: UUID
    agencia_id: UUID
    nome_arquivo: str
    created_at: datetime
    total_no_arquivo: int
    importadas: int
    duplicadas: int
    rejeitadas: int
    # Quantas transações do lote ainda existem. Diverge de `importadas` assim
    # que alguma é removida individualmente — é o que diz se ainda há o que
    # cancelar.
    transacoes_ativas: int = 0
    cancelada_em: datetime | None = None
    motivo_cancelamento: str | None = None

    model_config = {"from_attributes": True}


class ImportacaoListResponse(BaseModel):
    items: list[ImportacaoResponse]
    total: int
    page: int
    page_size: int


class CancelarImportacaoRequest(BaseModel):
    motivo: str = Field(..., min_length=3, max_length=300)

    @field_validator("motivo", mode="before")
    @classmethod
    def limpar_motivo(cls, valor: str) -> str:
        return valor.strip() if isinstance(valor, str) else valor


class CancelarImportacaoResponse(BaseModel):
    importacao_id: UUID
    transacoes_removidas: int
    lancamentos_cancelados: int
