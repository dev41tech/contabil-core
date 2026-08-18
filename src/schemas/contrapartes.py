"""Schemas Pydantic para Contrapartes (fornecedor/cliente identificado por CPF/CNPJ)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.core.cnpj import somente_digitos

TIPOS_CONTRAPARTE = ("fornecedor", "cliente", "ambos")


class ContraparteCreate(BaseModel):
    tipo: str = Field(..., description=f"Um de: {TIPOS_CONTRAPARTE}")
    documento: str = Field(..., description="CPF ou CNPJ, com ou sem máscara.")
    razao_social: str = Field(..., min_length=2, max_length=300)
    nome_fantasia: str | None = Field(default=None, max_length=300)
    conta_contabil_id: UUID

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TIPOS_CONTRAPARTE:
            raise ValueError(f"Tipo inválido. Opções: {TIPOS_CONTRAPARTE}")
        return v

    @field_validator("documento")
    @classmethod
    def valida_documento(cls, v: str) -> str:
        d = somente_digitos(v)
        if len(d) not in (11, 14):
            raise ValueError("documento deve ser um CPF (11 dígitos) ou CNPJ (14 dígitos).")
        return d

    @field_validator("razao_social", "nome_fantasia", mode="before")
    @classmethod
    def strip_nomes(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class ContraparteUpdate(BaseModel):
    tipo: str | None = None
    razao_social: str | None = Field(default=None, min_length=2, max_length=300)
    nome_fantasia: str | None = Field(default=None, max_length=300)
    conta_contabil_id: UUID | None = None
    ativa: bool | None = None

    @field_validator("tipo")
    @classmethod
    def valida_tipo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in TIPOS_CONTRAPARTE:
            raise ValueError(f"Tipo inválido. Opções: {TIPOS_CONTRAPARTE}")
        return v

    @field_validator("razao_social", "nome_fantasia", mode="before")
    @classmethod
    def strip_nomes(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class ContraparteResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    tipo: str
    documento: str
    razao_social: str
    nome_fantasia: str | None
    conta_contabil_id: UUID
    origem: str
    confirmado_em: datetime | None
    ativa: bool
    # Campos expandidos (preenchidos pelo service)
    conta_codigo: str | None = None
    conta_descricao: str | None = None

    model_config = {"from_attributes": True}


class ContraparteListResponse(BaseModel):
    items: list[ContraparteResponse]
    total: int
    page: int
    page_size: int
