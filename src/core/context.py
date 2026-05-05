"""Contexto de request: trace_id, user_id, company_id propagados via contextvars.

Todo middleware e service pode ler/escrever neste contexto.
structlog usa automaticamente via merge_contextvars.
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

import structlog

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_user_id: ContextVar[str] = ContextVar("user_id", default="-")
_company_id: ContextVar[str] = ContextVar("company_id", default="-")


def set_request_context(
    trace_id: str,
    user_id: str | UUID | None = None,
    company_id: str | UUID | None = None,
) -> None:
    """Chamado pelo middleware no início de cada request."""
    _trace_id.set(trace_id)
    _user_id.set(str(user_id) if user_id else "-")
    _company_id.set(str(company_id) if company_id else "-")

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        user_id=str(user_id) if user_id else "-",
        company_id=str(company_id) if company_id else "-",
    )


def get_trace_id() -> str:
    return _trace_id.get()


def get_user_id() -> str:
    return _user_id.get()


def get_company_id() -> str:
    return _company_id.get()
