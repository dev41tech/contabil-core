"""Dependências FastAPI reutilizáveis.

Uso nos routers:
    @router.get("/empresas")
    async def list_empresas(
        ctx: AuthContext = Depends(require_auth),
        db: AsyncSession = Depends(get_db),
    ):
        ...
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog
from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.context import set_request_context
from src.core.errors import AuthError, ForbiddenError, TenantAccessDeniedError
from src.core.security import COOKIE_ACCESS, CSRF_HEADER, decode_access_token
from src.db.models import Permissao, Usuario
from src.db.session import get_db

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    tenant_id: UUID
    role: str
    email: str


async def _get_current_user(
    access_token: str | None = Cookie(default=None, alias=COOKIE_ACCESS),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Decodifica o access token do cookie e retorna o contexto do usuário."""
    if not access_token:
        raise AuthError()

    payload = decode_access_token(access_token)

    user_id = UUID(payload["sub"])
    tenant_id = UUID(payload["tenant_id"])
    role = payload["role"]

    # Atualiza contexto de logs com user_id
    set_request_context(
        trace_id=structlog.contextvars.get_contextvars().get("trace_id", "-"),
        user_id=user_id,
    )

    # Verifica se o usuário ainda existe e está ativo
    result = await db.execute(
        select(Usuario).where(Usuario.id == user_id, Usuario.ativo == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise AuthError(message="Usuário não encontrado ou inativo.")

    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email=user.email,
    )


async def require_auth(
    ctx: AuthContext = Depends(_get_current_user),
) -> AuthContext:
    """Qualquer usuário autenticado."""
    return ctx


async def require_admin(
    ctx: AuthContext = Depends(_get_current_user),
) -> AuthContext:
    """Apenas admins do escritório."""
    if ctx.role != "admin":
        raise ForbiddenError(message="Apenas administradores podem executar esta ação.")
    return ctx


async def require_csrf(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """Valida CSRF token para requisições de mutação (POST/PUT/PATCH/DELETE)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    csrf_cookie = request.cookies.get("csrf_token")
    if not csrf_cookie or csrf_cookie != x_csrf_token:
        raise ForbiddenError(message="CSRF token inválido.")


async def get_company_context(
    empresa_id: UUID,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Verifica se o usuário tem acesso à empresa e retorna o AuthContext.

    O empresa_id é resolvido automaticamente pelo FastAPI a partir do path parameter
    `{empresa_id}` definido no router que usar esta dependência.
    """
    # Admin tem acesso a todas as empresas do tenant
    if ctx.role != "admin":
        result = await db.execute(
            select(Permissao).where(
                Permissao.usuario_id == ctx.user_id,
                Permissao.empresa_id == empresa_id,
            )
        )
        if not result.scalar_one_or_none():
            raise TenantAccessDeniedError()

    set_request_context(
        trace_id=structlog.contextvars.get_contextvars().get("trace_id", "-"),
        user_id=ctx.user_id,
        company_id=empresa_id,
    )
    return ctx
