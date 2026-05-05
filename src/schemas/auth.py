"""Schemas Pydantic para autenticação."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    tenant_id: UUID = Field(..., description="ID do escritório contábil")
    email: EmailStr
    senha: str = Field(..., min_length=8)


class LoginResponse(BaseModel):
    message: str = "Login realizado com sucesso."
    # Tokens trafegam em cookies HttpOnly — não no body.
    # O body retorna apenas o CSRF token para uso em mutações.
    csrf_token: str


class RefreshResponse(BaseModel):
    message: str = "Tokens renovados com sucesso."
    csrf_token: str


class MeResponse(BaseModel):
    user_id: UUID
    email: str
    nome: str
    role: str
    tenant_id: UUID
