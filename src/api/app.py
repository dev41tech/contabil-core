"""Factory da aplicação FastAPI."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.middleware import (
    AppError,
    RequestContextMiddleware,
    app_error_handler,
    unhandled_error_handler,
)
from src.api.v1 import router as v1_router
from src.core.config import get_settings
from src.core.logging import configure_logging

_startup_logger = structlog.get_logger("startup")


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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Eventos de ciclo de vida da aplicação."""
    _reset_stuck_processando()
    yield


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

    # ── Middleware (ordem importa — primeiro registrado = mais externo)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,  # necessário para cookies cross-origin
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)

    # ── Handlers de erro
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # ── Routers
    app.include_router(v1_router)

    @app.get("/api/health", tags=["infra"])
    async def health() -> dict:
        return {"status": "ok", "version": settings.app_version}

    return app
