"""Schemas Pydantic para Cartão de Crédito."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

BANDEIRAS = ("visa", "mastercard", "elo", "amex", "hipercard", "outros")
STATUS_FATURA = ("aberta", "fechada", "paga")


# ── Cartão ────────────────────────────────────────────────────────────────────

class CartaoCreate(BaseModel):
    nome: str = Field(..., min_length=2, max_length=200)
    bandeira: str = Field(..., description=f"Uma de: {BANDEIRAS}")
    ultimos_digitos: str | None = Field(default=None, min_length=4, max_length=4)
    dia_fechamento: int = Field(..., ge=1, le=28)
    dia_vencimento: int = Field(..., ge=1, le=28)
    limite: float | None = Field(default=None, ge=0)

    @field_validator("bandeira")
    @classmethod
    def valida_bandeira(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in BANDEIRAS:
            raise ValueError(f"Bandeira inválida. Opções: {BANDEIRAS}")
        return v

    @field_validator("ultimos_digitos", mode="before")
    @classmethod
    def valida_digitos(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v.isdigit() or len(v) != 4:
            raise ValueError("ultimos_digitos deve ter exatamente 4 dígitos numéricos.")
        return v


class CartaoUpdate(BaseModel):
    nome: str | None = Field(default=None, min_length=2, max_length=200)
    dia_fechamento: int | None = Field(default=None, ge=1, le=28)
    dia_vencimento: int | None = Field(default=None, ge=1, le=28)
    limite: float | None = None
    ativo: bool | None = None


class CartaoResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    nome: str
    bandeira: str
    ultimos_digitos: str | None
    dia_fechamento: int
    dia_vencimento: int
    limite: float | None
    ativo: bool
    total_faturas: int = 0
    fatura_aberta_valor: float | None = None

    model_config = {"from_attributes": True}


class CartaoListResponse(BaseModel):
    items: list[CartaoResponse]
    total: int


# ── Fatura ────────────────────────────────────────────────────────────────────

class FaturaCreate(BaseModel):
    competencia: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}$",
        description="Mês de competência. Ex: 2024-03",
    )
    data_fechamento: datetime | None = None
    data_vencimento: datetime | None = None
    observacao: str | None = Field(default=None, max_length=500)


class FaturaUpdate(BaseModel):
    status: str | None = None
    data_vencimento: datetime | None = None
    observacao: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def valida_status(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in STATUS_FATURA:
            raise ValueError(f"Status inválido. Opções: {STATUS_FATURA}")
        return v


class AssociarTransacaoFaturaRequest(BaseModel):
    transacao_id: UUID


class FaturaResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    cartao_id: UUID
    cartao_nome: str | None = None
    cartao_bandeira: str | None = None
    cartao_digitos: str | None = None
    competencia: str
    data_fechamento: datetime | None
    data_vencimento: datetime | None
    valor_total: float
    status: str
    transacao_id: UUID | None
    observacao: str | None
    total_lancamentos: int = 0

    model_config = {"from_attributes": True}


class FaturaListResponse(BaseModel):
    items: list[FaturaResponse]
    total: int


# ── Lançamento ────────────────────────────────────────────────────────────────

class LancamentoCreate(BaseModel):
    data_compra: datetime
    descricao: str = Field(..., min_length=1, max_length=500)
    valor: float = Field(..., gt=0)
    conta_id: UUID | None = None
    parcela_atual: int | None = Field(default=None, ge=1)
    parcela_total: int | None = Field(default=None, ge=1)


class LancamentoBulkImport(BaseModel):
    """Importação em massa via lista de lançamentos."""
    lancamentos: list[LancamentoCreate]


class LancamentoResponse(BaseModel):
    id: UUID
    fatura_id: UUID
    empresa_id: UUID
    data_compra: datetime
    descricao: str
    valor: float
    conta_id: UUID | None
    parcela_atual: int | None
    parcela_total: int | None

    model_config = {"from_attributes": True}


class LancamentoListResponse(BaseModel):
    items: list[LancamentoResponse]
    total: int
    valor_total: float


class ImportCSVResponse(BaseModel):
    importados: int
    duplicados: int = 0
    erros: list[str]
