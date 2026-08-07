"""Schemas Pydantic para agências bancárias."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from src.schemas.types import BancoSigla, NumeroAgencia, NumeroConta


class AgenciaCreate(BaseModel):
    banco_sigla: BancoSigla = Field(
        ...,
        min_length=2,
        max_length=20,
        description="Sigla ou código do banco (ex: BRADESCO, 341, NU)",
    )
    agencia: NumeroAgencia = Field(..., description="Número da agência")
    numero: NumeroConta = Field(..., description="Número da conta")
    digito: str | None = Field(default=None, max_length=5, description="Dígito verificador")

    @field_validator("digito", mode="before")
    @classmethod
    def normaliza_digito(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v if v else None


class AgenciaUpdate(BaseModel):
    banco_sigla: BancoSigla | None = None
    agencia: NumeroAgencia | None = None
    numero: NumeroConta | None = None
    digito: str | None = None
    ativa: bool | None = None

class AgenciaResponse(BaseModel):
    id: UUID
    empresa_id: UUID
    banco_sigla: str
    agencia: str
    numero: str
    digito: str | None
    ativa: bool
    descricao: str  # propriedade computada do model

    model_config = {"from_attributes": True}


class AgenciaListResponse(BaseModel):
    items: list[AgenciaResponse]
    total: int
