"""Schemas Pydantic para empresas."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


REGIME_CHOICES = ("simples_nacional", "lucro_presumido", "lucro_real")


class EmpresaCreate(BaseModel):
    razao_social: str = Field(..., min_length=2, max_length=300)
    cnpj: str = Field(..., description="CNPJ com ou sem formatação")
    regime_tributario: str

    @field_validator("cnpj", mode="before")
    @classmethod
    def normaliza_cnpj(cls, v: str) -> str:
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos.")
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"

    @field_validator("regime_tributario")
    @classmethod
    def valida_regime(cls, v: str) -> str:
        if v not in REGIME_CHOICES:
            raise ValueError(f"Regime inválido. Opções: {REGIME_CHOICES}")
        return v


class EmpresaUpdate(BaseModel):
    razao_social: str | None = Field(default=None, min_length=2, max_length=300)
    regime_tributario: str | None = None

    @field_validator("regime_tributario")
    @classmethod
    def valida_regime(cls, v: str | None) -> str | None:
        if v is not None and v not in REGIME_CHOICES:
            raise ValueError(f"Regime inválido. Opções: {REGIME_CHOICES}")
        return v


class EmpresaResponse(BaseModel):
    id: UUID
    razao_social: str
    cnpj: str
    regime_tributario: str
    ativa: bool

    model_config = {"from_attributes": True}


class EmpresaListResponse(BaseModel):
    items: list[EmpresaResponse]
    total: int
    page: int
    page_size: int
