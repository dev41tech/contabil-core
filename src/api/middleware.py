"""Middlewares globais da aplicação.

1. RequestContextMiddleware — injeta trace_id, user_id, company_id no contexto de logs
2. ErrorHandlerMiddleware — converte exceções não tratadas em JSON estruturado
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.core.context import set_request_context
from src.core.errors import AppError

logger = structlog.get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Gera trace_id único por request e injeta no contexto de logs."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = request.headers.get("X-Trace-ID") or secrets.token_hex(8)

        # user_id e company_id são preenchidos pelo dep. de auth depois
        set_request_context(trace_id=trace_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        response.headers["X-Trace-ID"] = trace_id

        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )

        return response


def _error_response(error: AppError) -> JSONResponse:
    body: dict[str, Any] = {
        "error": error.code,
        "message": error.message,
    }
    if error.details:
        body["details"] = error.details
    return JSONResponse(status_code=error.http_status, content=body)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Handler para erros tipados de domínio."""
    if exc.http_status >= 500:
        logger.error("app.error", error_code=exc.code, message=exc.message, exc_info=exc)
    else:
        logger.warning("app.error", error_code=exc.code, message=exc.message)
    return _error_response(exc)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handler de último recurso — nunca retorna 500 sem contexto."""
    logger.exception("unhandled.error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "Erro interno. Nossa equipe foi notificada.",
        },
    )
