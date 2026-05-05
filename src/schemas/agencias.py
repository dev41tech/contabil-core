"""Schemas Pydantic para agências bancárias."""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# Lista dos bancos mais comuns no Brasil — usada apenas para sugestão de sigla,
# não bloqueia siglas fora da lista.
_BANCOS_CONHECIDOS: dict[str, str] = {
    "001": "BB",
    "033": "SANTANDER",
    "077": "INTER",
    "104": "CEF",
    "237": "BRADESCO",
    "341": "ITAU",
    "356": "BMG",
    "389": "MERCANTIL",
    "422": "SAFRA",
    "745": "CITIBANK",
    "756": "SICOOB",
    "084": "UNIPRIME",
    "748": "SICREDI",
    "336": "C6",
    "260": "NU",
    "290": "PAGSEGURO",
}


class AgenciaCreate(BaseModel):
    banco_sigla: str = Field(
        ...,
        min_length=2,
        max_length=20,
        description="Sigla ou código do banco (ex: BRADESCO, 341, NU)",
    )
    agencia: str = Field(..., min_length=1, max_length=10, description="Número da agência")
    numero: str = Field(..., min_length=1, max_length=20, description="Número da conta")
    digito: str | None = Field(default=None, max_length=5, description="Dígito verificador")

    @field_validator("banco_sigla", mode="before")
    @classmethod
    def normaliza_sigla(cls, v: str) -> str:
        v = v.strip().upper()
        # Se for código numérico de 3 dígitos e estiver na lista, converte para sigla
        return _BANCOS_CONHECIDOS.get(v, v)

    @field_validator("agencia", "numero", mode="before")
    @classmethod
    def remove_espacos(cls, v: str) -> str:
        return v.strip()

    @field_validator("agencia")
    @classmethod
    def valida_agencia(cls, v: str) -> str:
        if not re.match(r"^\d{1,10}$", v):
            raise ValueError("Agência deve conter apenas dígitos (máx. 10).")
        return v

    @field_validator("numero")
    @classmethod
    def valida_numero(cls, v: str) -> str:
        # Conta bancária pode ter letras em alguns bancos (ex: BB empresa)
        if not re.match(r"^[\d\w\-]{1,20}$", v):
            raise ValueError("Número da conta inválido.")
        return v

    @field_validator("digito", mode="before")
    @classmethod
    def normaliza_digito(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v if v else None


class AgenciaUpdate(BaseModel):
    banco_sigla: str | None = Field(default=None, min_length=2, max_length=20)
    agencia: str | None = Field(default=None, min_length=1, max_length=10)
    numero: str | None = Field(default=None, min_length=1, max_length=20)
    digito: str | None = None
    ativa: bool | None = None

    @field_validator("banco_sigla", mode="before")
    @classmethod
    def normaliza_sigla(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        return _BANCOS_CONHECIDOS.get(v, v)

    @field_validator("agencia", mode="before")
    @classmethod
    def valida_agencia(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^\d{1,10}$", v):
            raise ValueError("Agência deve conter apenas dígitos (máx. 10).")
        return v


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
