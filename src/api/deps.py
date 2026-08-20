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
from src.core.security import COOKIE_ACCESS, decode_access_token
from src.db.models import Empresa, Permissao, Tenant, Usuario
from src.db.session import get_db

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
    # Atualiza contexto de logs com user_id
    set_request_context(
        trace_id=structlog.contextvars.get_contextvars().get("trace_id", "-"),
        user_id=user_id,
    )

    # Verifica se o usuário ainda existe e está ativo
    result = await db.execute(
        select(Usuario)
        .join(Tenant, Tenant.id == Usuario.tenant_id)
        .where(
            Usuario.id == user_id,
            Usuario.tenant_id == tenant_id,
            Usuario.ativo == True,
            Tenant.ativo == True,
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        raise AuthError(message="Usuário não encontrado ou inativo.")

    return AuthContext(
        user_id=user_id,
        tenant_id=user.tenant_id,
        role=user.role,
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
    request: Request,
    empresa_id: UUID,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Verifica se o usuário tem acesso à empresa e retorna o AuthContext.

    O empresa_id é resolvido automaticamente pelo FastAPI a partir do path parameter
    `{empresa_id}` definido no router que usar esta dependência.
    """
    # A empresa sempre precisa pertencer ao tenant autenticado e estar ativa,
    # inclusive quando o usuário é admin.
    empresa = (
        await db.execute(
            select(Empresa).where(
                Empresa.id == empresa_id,
                Empresa.tenant_id == ctx.tenant_id,
                Empresa.ativa == True,
                Empresa.deleted_at == None,
            )
        )
    ).scalar_one_or_none()
    if not empresa:
        raise TenantAccessDeniedError()

    # O módulo é o primeiro segmento após /empresas/{empresa_id}. Manter a
    # resolução aqui evita espalhar dependências distintas por todos os routers.
    partes = request.url.path.strip("/").split("/")
    try:
        modulo = partes[partes.index("empresas") + 2].replace("-", "_")
    except (ValueError, IndexError):
        modulo = ""

    # Admin tem acesso a todos os módulos, mas não cruza a fronteira do tenant.
    if ctx.role != "admin":
        result = await db.execute(
            select(Permissao).where(
                Permissao.usuario_id == ctx.user_id,
                Permissao.empresa_id == empresa_id,
            )
        )
        permissao = result.scalar_one_or_none()
        modulos = (
            {m.strip().lower() for m in permissao.modulos.split(",") if m.strip()}
            if permissao
            else set()
        )
        modulos_aceitos = {modulo}
        if modulo == "jobs":
            # Quem pode iniciar NEO/extrato precisa acompanhar o job resultante.
            # A rota de jobs ainda filtra cada tipo para não expor o outro módulo.
            modulos_aceitos.update({"neo", "extrato"})
        if not permissao or (
            "*" not in modulos and modulos.isdisjoint(modulos_aceitos)
        ):
            raise TenantAccessDeniedError()

    set_request_context(
        trace_id=structlog.contextvars.get_contextvars().get("trace_id", "-"),
        user_id=ctx.user_id,
        company_id=empresa_id,
    )
    return ctx
