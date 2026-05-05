"""Router de autenticação — /api/v1/auth"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Cookie, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth
from src.core.config import get_settings
from src.core.errors import AuthError
from src.core.security import (
    COOKIE_ACCESS,
    COOKIE_REFRESH,
    generate_csrf_token,
    make_cookie_kwargs,
)
from src.db.models import Tenant, Usuario
from src.db.session import get_db
from src.domain.auth.service import AuthService
from src.schemas.auth import LoginRequest, LoginResponse, MeResponse, RefreshResponse
from sqlalchemy import select

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> str:
    """Define os cookies de autenticação e retorna o CSRF token."""
    settings = get_settings()
    kwargs = make_cookie_kwargs()

    response.set_cookie(
        key=COOKIE_ACCESS,
        value=access_token,
        max_age=settings.access_token_ttl_minutes * 60,
        **kwargs,
    )
    response.set_cookie(
        key=COOKIE_REFRESH,
        value=refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path="/api/v1/auth/refresh",  # Refresh token só vai para este endpoint
        **kwargs,
    )

    csrf = generate_csrf_token()
    # CSRF token em cookie NÃO HttpOnly — precisa ser lido pelo frontend
    response.set_cookie(
        key="csrf_token",
        value=csrf,
        max_age=settings.access_token_ttl_minutes * 60,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    return csrf


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(COOKIE_ACCESS)
    response.delete_cookie(COOKIE_REFRESH, path="/api/v1/auth/refresh")
    response.delete_cookie("csrf_token")


@router.post("/login", response_model=LoginResponse, status_code=200)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Autentica o usuário e define cookies HttpOnly."""
    service = AuthService(db)
    access_token, refresh_token = await service.login(
        email=body.email,
        senha=body.senha,
        tenant_id=body.tenant_id,
    )
    csrf = _set_auth_cookies(response, access_token, refresh_token)
    return LoginResponse(csrf_token=csrf)


@router.post("/refresh", response_model=RefreshResponse, status_code=200)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=COOKIE_REFRESH),
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Renova access + refresh tokens usando o refresh token do cookie."""
    if not refresh_token:
        raise AuthError(message="Refresh token ausente.")

    service = AuthService(db)
    new_access, new_refresh = await service.refresh(refresh_token)
    csrf = _set_auth_cookies(response, new_access, new_refresh)
    return RefreshResponse(csrf_token=csrf)


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=COOKIE_REFRESH),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoga a sessão e limpa os cookies."""
    if refresh_token:
        service = AuthService(db)
        await service.logout(refresh_token)
    _clear_auth_cookies(response)


@router.get("/me", response_model=MeResponse)
async def me(
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """Retorna dados do usuário autenticado."""
    result = await db.execute(select(Usuario).where(Usuario.id == ctx.user_id))
    user = result.scalar_one()
    return MeResponse(
        user_id=user.id,
        email=user.email,
        nome=user.nome,
        role=user.role,
        tenant_id=ctx.tenant_id,
    )


@router.get("/tenant")
async def buscar_tenant_por_cnpj(
    cnpj: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Endpoint público: resolve CNPJ → tenant_id para a tela de login."""
    result = await db.execute(select(Tenant).where(Tenant.cnpj == cnpj))
    tenant = result.scalar_one_or_none()
    if not tenant:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Escritório não encontrado para este CNPJ.")
    return {"id": str(tenant.id), "nome": tenant.nome}
