"""Schemas Pydantic para empresas."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.schemas.types import CNPJ


REGIME_CHOICES = ("simples_nacional", "lucro_presumido", "lucro_real")


class EmpresaCreate(BaseModel):
    razao_social: str = Field(..., min_length=2, max_length=300)
    cnpj: CNPJ = Field(..., description="CNPJ com ou sem formatação")
    regime_tributario: str

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


class EmpresaCnpjInvalidoResponse(BaseModel):
    """Uma empresa cujo CNPJ cadastrado não passa na validação de dígito verificador."""

    id: UUID
    razao_social: str
    cnpj: str

    model_config = {"from_attributes": True}


class CnpjInvalidoListResponse(BaseModel):
    items: list[EmpresaCnpjInvalidoResponse]
    total: int
    total_empresas: int
    aviso: str = (
        "A exportação de notas fiscais destas empresas não funciona: o filtro compara "
        "o CNPJ cadastrado com o emitente/destinatário da nota e um CNPJ inexistente "
        "nunca casa."
    )
