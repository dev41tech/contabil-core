"""Schemas Pydantic para Regras de Categorização."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RegraCreate(BaseModel):
    conta_id: UUID
    agencia_id: UUID
    descricao: str = Field(..., min_length=2, max_length=500)
    historico: str = Field(
        ...,
        min_length=2,
        max_length=500,
        description="Texto do extrato bancário que dispara esta regra.",
    )
    dc: str = Field(..., description="D = Débito, C = Crédito")
    tipo: str = Field(..., description="automatica ou manual")
    manter_historico: bool = False

    @field_validator("dc")
    @classmethod
    def valida_dc(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("D", "C"):
            raise ValueError("dc deve ser 'D' (Débito) ou 'C' (Crédito).")
        return v

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("automatica", "manual"):
            raise ValueError("tipo deve ser 'automatica' ou 'manual'.")
        return v

    @field_validator("historico", "descricao", mode="before")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


class RegraUpdate(BaseModel):
    descricao: str | None = Field(default=None, min_length=2, max_length=500)
    dc: str | None = None
    tipo: str | None = None
    manter_historico: bool | None = None
    ativa: bool | None = None

    @field_validator("dc")
    @classmethod
    def valida_dc(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in ("D", "C"):
            raise ValueError("dc deve ser 'D' ou 'C'.")
        return v

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in ("automatica", "manual"):
            raise ValueError("tipo deve ser 'automatica' ou 'manual'.")
        return v


class RegraResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    conta_id: UUID
    agencia_id: UUID
    descricao: str
    historico: str
    dc: str
    tipo: str
    manter_historico: bool
    ativa: bool
    # Campos expandidos (opcionais — preenchidos pelo service)
    conta_codigo: str | None = None
    conta_descricao: str | None = None
    agencia_descricao: str | None = None

    model_config = {"from_attributes": True}


class RegraListResponse(BaseModel):
    items: list[RegraResponse]
    total: int
    page: int
    page_size: int
