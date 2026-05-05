"""Endpoint de bootstrap — cria o primeiro tenant + admin.

Só funciona se não houver nenhum tenant cadastrado.
Desabilitar em produção via ENABLE_SETUP_ENDPOINT=false.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import hash_password
from src.db.models import Tenant, Usuario
from src.db.session import get_db

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupRequest(BaseModel):
    tenant_nome: str = Field(..., min_length=3, max_length=200)
    tenant_cnpj: str = Field(..., description="14 dígitos ou formatado")
    admin_nome: str = Field(..., min_length=2, max_length=200)
    admin_email: EmailStr
    admin_senha: str = Field(..., min_length=8)

    @field_validator("tenant_cnpj", mode="before")
    @classmethod
    def normaliza_cnpj(cls, v: str) -> str:
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos.")
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


class SetupResponse(BaseModel):
    tenant_id: uuid.UUID
    usuario_id: uuid.UUID
    email: str
    mensagem: str


@router.post("", response_model=SetupResponse, status_code=201)
async def setup_inicial(
    body: SetupRequest,
    db: AsyncSession = Depends(get_db),
) -> SetupResponse:
    """Cria o primeiro tenant e usuário admin do sistema.

    Retorna 409 se já houver tenants cadastrados.
    """
    total = (await db.execute(select(func.count()).select_from(Tenant))).scalar_one()
    if total > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "SETUP_JA_REALIZADO",
                "message": "Sistema já foi configurado. Use o login normal.",
            },
        )

    tenant = Tenant(
        id=uuid.uuid4(),
        nome=body.tenant_nome,
        cnpj=body.tenant_cnpj,
        plano="basico",
    )
    db.add(tenant)
    await db.flush()

    admin = Usuario(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=body.admin_email,
        nome=body.admin_nome,
        senha_hash=hash_password(body.admin_senha),
        role="admin",
    )
    db.add(admin)
    await db.commit()

    return SetupResponse(
        tenant_id=tenant.id,
        usuario_id=admin.id,
        email=admin.email,
        mensagem="Sistema configurado com sucesso. Faça login para continuar.",
    )
