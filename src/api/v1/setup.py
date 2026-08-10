"""Endpoint de bootstrap — cria o primeiro tenant + admin.

Só é montado quando ENABLE_SETUP_ENDPOINT=true e SETUP_BOOTSTRAP_SECRET está
configurado. O segredo deve ser enviado no header X-Setup-Token.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import hash_password
from src.db.models import Tenant, Usuario
from src.db.session import get_db
from src.schemas.types import CNPJ

router = APIRouter(prefix="/setup", tags=["setup"])

# O advisory lock cobre todos os processos no PostgreSQL. Este lock também
# serializa testes/execuções locais em bancos que não oferecem advisory locks.
_setup_lock = asyncio.Lock()
_SETUP_ADVISORY_LOCK_ID = 41004100


class SetupRequest(BaseModel):
    tenant_nome: str = Field(..., min_length=3, max_length=200)
    tenant_cnpj: CNPJ = Field(..., description="14 dígitos ou formatado")
    admin_nome: str = Field(..., min_length=2, max_length=200)
    admin_email: EmailStr
    admin_senha: str = Field(..., min_length=8)

class SetupResponse(BaseModel):
    tenant_id: uuid.UUID
    usuario_id: uuid.UUID
    email: str
    mensagem: str


@router.post("", response_model=SetupResponse, status_code=201)
async def setup_inicial(
    body: SetupRequest,
    x_setup_token: str | None = Header(default=None, alias="X-Setup-Token"),
    db: AsyncSession = Depends(get_db),
) -> SetupResponse:
    """Cria o primeiro tenant e usuário admin do sistema.

    Retorna 409 se já houver tenants cadastrados.
    """
    settings = get_settings()
    expected = settings.setup_bootstrap_secret
    if (
        expected is None
        or not x_setup_token
        or not secrets.compare_digest(x_setup_token, expected.get_secret_value())
    ):
        raise HTTPException(status_code=403, detail="Token de bootstrap inválido.")

    async with _setup_lock:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            # SELECT FOR UPDATE não bloqueia uma tabela vazia. O advisory lock
            # transacional garante um único bootstrap mesmo sem linhas existentes.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _SETUP_ADVISORY_LOCK_ID},
            )

        existente = (await db.execute(select(Tenant.id).limit(1))).scalar_one_or_none()
        if existente is not None:
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
        # Commit dentro da região serializada: o lock só pode ser liberado depois
        # que o primeiro setup já estiver visível às requisições concorrentes.
        await db.commit()

    return SetupResponse(
        tenant_id=tenant.id,
        usuario_id=admin.id,
        email=admin.email,
        mensagem="Sistema configurado com sucesso. Faça login para continuar.",
    )
