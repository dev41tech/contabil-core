"""Registro transacional e serialização segura de mutações auditáveis."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.context import get_trace_id, get_user_id
from src.db.models import AuditLog, Empresa


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError(f"Tipo não serializável na auditoria: {type(value).__name__}")


def _serializar(dados: Mapping[str, object] | None) -> str | None:
    if dados is None:
        return None
    return json.dumps(
        dict(dados),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _usuario_do_contexto() -> UUID | None:
    try:
        return UUID(get_user_id())
    except (ValueError, TypeError, AttributeError):
        return None


async def registrar_auditoria(
    db: AsyncSession,
    *,
    acao: str,
    entidade: str,
    entidade_id: UUID | str | int | None,
    dados_antes: Mapping[str, object] | None = None,
    dados_depois: Mapping[str, object] | None = None,
    tenant_id: UUID | None = None,
    empresa_id: UUID | None = None,
    usuario_id: UUID | None = None,
    ip: str | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    """Adiciona o log à mesma sessão/transação da mutação e força seu flush."""
    if tenant_id is None and empresa_id is not None:
        tenant_id = (
            await db.execute(select(Empresa.tenant_id).where(Empresa.id == empresa_id))
        ).scalar_one_or_none()
    if tenant_id is None:
        raise ValueError("tenant_id ou empresa_id é obrigatório para registrar auditoria.")

    log = AuditLog(
        tenant_id=tenant_id,
        usuario_id=usuario_id or _usuario_do_contexto(),
        empresa_id=empresa_id,
        acao=acao[:100],
        entidade=entidade[:100],
        entidade_id=str(entidade_id)[:50] if entidade_id is not None else None,
        dados_antes=_serializar(dados_antes),
        dados_depois=_serializar(dados_depois),
        ip=ip,
        trace_id=(trace_id or get_trace_id())[:64],
    )
    db.add(log)
    await db.flush()
    return log
