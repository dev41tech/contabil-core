"""Schemas Pydantic para gestão de usuários."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class UsuarioCreate(BaseModel):
    nome: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    senha: str = Field(..., min_length=8, description="Senha inicial do usuário")
    role: str = Field(default="contador")

    @field_validator("role")
    @classmethod
    def valida_role(cls, v: str) -> str:
        if v not in ("admin", "contador"):
            raise ValueError("Role inválida. Use 'admin' ou 'contador'.")
        return v


class UsuarioResponse(BaseModel):
    id: UUID
    nome: str
    email: str
    role: str
    ativo: bool

    model_config = {"from_attributes": True}


class UsuarioListResponse(BaseModel):
    items: list[UsuarioResponse]
    total: int
    page: int
    page_size: int
