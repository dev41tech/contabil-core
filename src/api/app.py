"""Factory da aplicação FastAPI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import (
    AppError,
    LimiteUploadMiddleware,
    RequestContextMiddleware,
    app_error_handler,
    unhandled_error_handler,
)
from src.api.v1 import router as v1_router
from src.api.v1.setup import router as setup_router
from src.core.config import get_settings
from src.core.logging import configure_logging
from src.core.rate_limit import LoginRateLimiter
from src.db.session import get_db

_startup_logger = structlog.get_logger("startup")
_health_logger = structlog.get_logger("health")

# Curto de propósito: o health check é chamado a cada 30s pelo orquestrador e
# não pode ficar pendurado num banco que parou de responder.
_HEALTH_DB_TIMEOUT_S = 2.0


async def _checar_banco(db: AsyncSession) -> str:
    """Retorna "ok" se o banco respondeu ao SELECT 1 dentro do timeout, senão "error"."""
    try:
        async with asyncio.timeout(_HEALTH_DB_TIMEOUT_S):
            await db.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        _health_logger.warning("health.banco_indisponivel", erro=str(exc))
        # Sem o rollback a sessão volta quebrada para o get_db, que tentaria
        # commitar e transformaria o 503 num 500.
        try:
            await db.rollback()
        except Exception:
            pass
        return "error"


def _reset_stuck_processando() -> None:
    """Reseta arquivos CONCILPRO travados em PROCESSANDO (0 fornecedores) para ERRO.

    Chamado no startup — garante que arquivos que estavam sendo processados quando
    o servidor foi encerrado apareçam como ERRO no frontend, não como "processando".
    """
    try:
        from src.db.session import SyncSessionLocal
        from src.db.models import CpArquivo

        db = SyncSessionLocal()
        try:
            travados = (
                db.query(CpArquivo)
                .filter(CpArquivo.status == "PROCESSANDO", CpArquivo.total_fornecedores == 0)
                .all()
            )
            if travados:
                for a in travados:
                    a.status = "ERRO"
                    a.mensagem_erro = (
                        "Processamento interrompido (servidor reiniciado). "
                        "Faça o upload novamente para reprocessar."
                    )
                db.commit()
                _startup_logger.warning(
                    "concilpro.startup.reset_stuck",
                    count=len(travados),
                    ids=[a.id for a in travados],
                )
        finally:
            db.close()
    except Exception as exc:
        _startup_logger.warning("concilpro.startup.reset_stuck_failed", error=str(exc))


async def _recuperar_jobs_sem_heartbeat() -> None:
    """Recupera jobs realmente órfãos sem interferir nos demais workers."""
    try:
        from src.domain.jobs import recuperar_jobs_sem_heartbeat

        recuperados = await recuperar_jobs_sem_heartbeat()
        if recuperados:
            _startup_logger.warning("jobs.startup.recuperados", count=recuperados)
    except Exception as exc:
        # Falha de manutenção não deve impedir o serviço de subir; o próximo
        # worker/startup terá outra oportunidade de executar o mesmo predicado.
        _startup_logger.warning("jobs.startup.recuperacao_falhou", error=str(exc))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Eventos de ciclo de vida da aplicação."""
    _reset_stuck_processando()
    await _recuperar_jobs_sem_heartbeat()
    yield
    rate_limiter = getattr(app.state, "login_rate_limiter", None)
    if rate_limiter is not None:
        await rate_limiter.close()


def create_app() -> FastAPI:
    settings = get_settings()

    configure_logging(
        json_logs=settings.is_production,
        log_level="DEBUG" if settings.debug else "INFO",
    )

    app = FastAPI(
        title="Contabil Core API",
        version=settings.app_version,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url="/api/redoc" if not settings.is_production else None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
        lifespan=_lifespan,
    )
    app.state.login_rate_limiter = LoginRateLimiter(settings)

    # ── Middleware (ordem importa — primeiro registrado = mais externo)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,  # necessário para cookies cross-origin
        allow_methods=["*"],
        allow_headers=["*"],
        # Sem isto o navegador NÃO entrega estes cabeçalhos ao JS em requisição
        # cross-origin — e o frontend está em outra origem. Os dois primeiros já
        # eram enviados pela exportação e nunca chegaram a ser lidos.
        expose_headers=[
            "X-Total-Registros",
            "X-Job-Id",
            "X-Contas-Sem-Codigo-Abreviado",
        ],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(LimiteUploadMiddleware, max_bytes=settings.max_upload_bytes)

    # ── Handlers de erro
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # ── Routers
    app.include_router(v1_router)
    if settings.enable_setup_endpoint:
        app.include_router(setup_router, prefix="/api/v1")

    @app.get("/api/health", tags=["infra"])
    async def health(response: Response, db: AsyncSession = Depends(get_db)) -> dict:
        """Readiness — 200 só quando a aplicação consegue falar com o banco.

        Um `{"status":"ok"}` estático não distinguia "container de pé" de
        "container sem banco". Devolve 503 quando o `SELECT 1` falha ou demora
        mais que `_HEALTH_DB_TIMEOUT_S`, para o painel não dar verde a um
        container que não atende request nenhum.
        """
        banco = await _checar_banco(db)
        if banco != "ok":
            response.status_code = 503
        return {
            "status": "ok" if banco == "ok" else "degraded",
            "version": settings.app_version,
            "commit": settings.git_commit,
            "database": banco,
        }

    @app.get("/api/health/live", tags=["infra"])
    async def health_live() -> dict:
        """Liveness — só diz que o processo responde. Não toca no banco.

        É o alvo do HEALTHCHECK do Docker: uma indisponibilidade momentânea do
        Postgres deve aparecer no `/api/health`, não derrubar o container.
        """
        return {
            "status": "ok",
            "version": settings.app_version,
            "commit": settings.git_commit,
        }

    return app
