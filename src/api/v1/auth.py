"""Router de autenticação — /api/v1/auth"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth, require_csrf
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
from src.domain.auditoria import registrar_auditoria
from src.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshResponse,
    TrocarSenhaRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str,
                      csrf_atual: str | None = None) -> str:
    """Define os cookies da sessão e devolve o token CSRF em vigor.

    `csrf_atual` é o token que o navegador JÁ tem. Quando ele vem, é preservado:
    ver o comentário no `set_cookie` do csrf abaixo.
    """
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

    # O cookie de CSRF é um só para o navegador inteiro; o token que o frontend
    # guarda é um por aba. Enquanto o refresh emitia um token novo, toda aba que
    # não tinha feito aquele refresh continuava mandando o valor anterior no
    # header e levava "CSRF token inválido" na primeira mutação — a cada 15
    # minutos, com o sistema aberto em duas partes.
    #
    # Rotacionar aqui não protegia nada: o valor é um número aleatório de
    # dupla submissão, não uma credencial, e o que ele prova é que quem mandou o
    # header conseguiu ler o cookie. Quem precisa de token novo é o LOGIN — aí
    # sim, um cookie plantado antes da autenticação não pode sobreviver a ela.
    csrf = csrf_atual or generate_csrf_token()
    # CSRF token em cookie NÃO HttpOnly — precisa ser lido pelo frontend.
    #
    # A validade acompanha o REFRESH, não o access token: com os 15 minutos do
    # access, uma aba parada perdia o cookie e a próxima mutação virava 403
    # "CSRF token inválido" em vez do 401 que dispararia a renovação. O logout
    # apaga o cookie, então durar mais não estende sessão nenhuma.
    response.set_cookie(
        key="csrf_token",
        value=csrf,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
    )
    return csrf


def _clear_auth_cookies(response: Response) -> None:
    # `domain` precisa repetir o que foi usado para GRAVAR: o navegador só
    # apaga o cookie quando escopo e caminho batem. Enquanto o delete omitia o
    # domínio, bastaria alguém preencher `COOKIE_DOMAIN` para o logout deixar a
    # sessão de pé — hoje ele é None em produção, e é justamente por isso que
    # essa assimetria passava despercebida.
    settings = get_settings()
    response.delete_cookie(COOKIE_ACCESS, domain=settings.cookie_domain)
    response.delete_cookie(COOKIE_REFRESH, path="/api/v1/auth/refresh",
                           domain=settings.cookie_domain)
    response.delete_cookie("csrf_token", domain=settings.cookie_domain)


@router.post("/login", response_model=LoginResponse, status_code=200)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Autentica o usuário e define cookies HttpOnly."""
    client_ip = request.client.host if request.client else "unknown"
    await request.app.state.login_rate_limiter.check(
        ip=client_ip,
        tenant_id=body.tenant_id,
        identity=body.email,
    )
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
    csrf_atual: str | None = Cookie(default=None, alias="csrf_token"),
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Renova access + refresh tokens usando o refresh token do cookie.

    O CSRF em vigor atravessa a renovação: ele é do navegador, não desta aba.
    """
    if not refresh_token:
        raise AuthError(message="Refresh token ausente.")

    service = AuthService(db)
    new_access, new_refresh = await service.refresh(refresh_token)
    csrf = _set_auth_cookies(response, new_access, new_refresh, csrf_atual=csrf_atual)
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


@router.post("/senha", status_code=204, dependencies=[Depends(require_csrf)])
async def trocar_senha(
    body: TrocarSenhaRequest,
    request: Request,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Troca a senha do próprio usuário e derruba as demais sessões.

    Derrubar as sessões é o ponto: trocar a senha por suspeita de vazamento
    não serve de nada se quem tem a senha antiga continua com a sessão viva.
    A sessão que fez a troca também cai — o cliente refaz o login, que é o
    comportamento esperado e o mais simples de explicar.
    """
    sessoes = await AuthService(db).trocar_senha(
        ctx.user_id, body.senha_atual, body.nova_senha
    )
    await registrar_auditoria(
        db,
        tenant_id=ctx.tenant_id,
        usuario_id=ctx.user_id,
        acao="usuario.senha_trocada",
        entidade="usuario",
        entidade_id=ctx.user_id,
        dados_depois={"sessoes_revogadas": sessoes},
        ip=request.client.host if request.client else None,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """Retorna dados do usuário autenticado, e o token de CSRF em vigor.

    O token vai junto porque esta é a única rota que uma aba recém-aberta com
    sessão válida chama — ela não passa por login nem por refresh, e não pode
    ler o cookie de outra origem.
    """
    result = await db.execute(select(Usuario).where(Usuario.id == ctx.user_id))
    user = result.scalar_one()
    return MeResponse(
        user_id=user.id,
        email=user.email,
        nome=user.nome,
        role=user.role,
        tenant_id=ctx.tenant_id,
        csrf_token=request.cookies.get("csrf_token"),
    )


@router.get("/tenant")
async def buscar_tenant_por_cnpj(
    cnpj: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Endpoint público: resolve CNPJ → tenant_id para a tela de login."""
    result = await db.execute(select(Tenant).where(Tenant.cnpj == cnpj, Tenant.ativo == True))
    tenant = result.scalar_one_or_none()
    if not tenant:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Escritório não encontrado para este CNPJ.")
    return {"id": str(tenant.id), "nome": tenant.nome}
