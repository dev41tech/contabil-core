"""Schemas Pydantic para Extrato Bancário."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class TransacaoResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    agencia_id: UUID
    data: datetime
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
    data_de: datetime | None = None
    data_ate: datetime | None = None
