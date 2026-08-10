"""Schemas Pydantic para Aplicações Financeiras."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

TIPOS_APLICACAO = ("cdb", "poupanca", "fundo", "tesouro_direto", "lci_lca", "outros")


class AplicacaoFinanceiraCreate(BaseModel):
    agencia_id: UUID | None = Field(
        default=None, description="Conta bancária vinculada, se houver."
    )
    instituicao: str = Field(..., min_length=2, max_length=200)
    tipo: str = Field(..., description=f"Um de: {TIPOS_APLICACAO}")
    descricao: str | None = Field(default=None, max_length=300)
    valor_aplicado: Decimal = Field(..., gt=0)
    data_aplicacao: datetime
    valor_atual: Decimal | None = Field(
        default=None, ge=0, description="Valor atualizado, se já conhecido no cadastro."
    )
    data_vencimento: datetime | None = None
    observacao: str | None = Field(default=None, max_length=500)

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TIPOS_APLICACAO:
            raise ValueError(f"Tipo inválido. Opções: {TIPOS_APLICACAO}")
        return v

    @field_validator("instituicao", mode="before")
    @classmethod
    def strip_instituicao(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class AplicacaoFinanceiraUpdate(BaseModel):
    agencia_id: UUID | None = None
    instituicao: str | None = Field(default=None, min_length=2, max_length=200)
    tipo: str | None = None
    descricao: str | None = Field(default=None, max_length=300)
    valor_atual: Decimal | None = Field(
        default=None, ge=0, description="Atualiza o valor/rendimento corrente."
    )
    data_vencimento: datetime | None = None
    observacao: str | None = Field(default=None, max_length=500)
    ativa: bool | None = Field(
        default=None, description="False = resgatada/encerrada."
    )

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in TIPOS_APLICACAO:
            raise ValueError(f"Tipo inválido. Opções: {TIPOS_APLICACAO}")
        return v


class AplicacaoFinanceiraResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    agencia_id: UUID | None
    instituicao: str
    tipo: str
    descricao: str | None
    valor_aplicado: Decimal
    data_aplicacao: datetime
    valor_atual: Decimal | None
    data_atualizacao_valor: datetime | None
    data_vencimento: datetime | None
    observacao: str | None
    ativa: bool
    rendimento: Decimal | None  # propriedade computada

    model_config = {"from_attributes": True}


class AplicacaoFinanceiraListResponse(BaseModel):
    items: list[AplicacaoFinanceiraResponse]
    total: int
    valor_total_aplicado: Decimal
    valor_total_atual: Decimal
