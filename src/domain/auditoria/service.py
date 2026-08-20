"""Registro transacional e serialização segura de mutações auditáveis."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.context import get_trace_id, get_user_id
from src.core.periodos import limites_competencia
from src.db.models import AuditLog, Empresa, Usuario
from src.schemas.auditoria import AuditoriaItemResponse, AuditoriaListResponse


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


def _desserializar(dados: str | None) -> dict[str, Any] | None:
    return json.loads(dados) if dados is not None else None


async def listar_auditoria(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    empresa_id: UUID,
    usuario_id: UUID | None = None,
    acao: str | None = None,
    entidade: str | None = None,
    mes: str | None = None,
    data_de: date | None = None,
    data_ate: date | None = None,
    page: int = 1,
    page_size: int = 50,
) -> AuditoriaListResponse:
    """Lista eventos da empresa com o ator resolvido na mesma consulta."""
    filtros = [
        AuditLog.tenant_id == tenant_id,
        AuditLog.empresa_id == empresa_id,
    ]
    if usuario_id is not None:
        filtros.append(AuditLog.usuario_id == usuario_id)
    if acao is not None:
        filtros.append(AuditLog.acao == acao)
    if entidade is not None:
        filtros.append(AuditLog.entidade == entidade)
    if mes is not None:
        inicio_mes, fim_mes = limites_competencia(mes)
        filtros.extend((AuditLog.created_at >= inicio_mes, AuditLog.created_at < fim_mes))
    if data_de is not None:
        filtros.append(AuditLog.created_at >= datetime.combine(data_de, time.min, tzinfo=UTC))
    if data_ate is not None:
        # Limite exclusivo no dia seguinte inclui qualquer horário de `data_ate`
        # sem depender da precisão de timestamp de SQLite ou PostgreSQL.
        proximo_dia = datetime.combine(data_ate, time.min, tzinfo=UTC) + timedelta(days=1)
        filtros.append(AuditLog.created_at < proximo_dia)

    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(*filtros))
    ).scalar_one()
    consulta = (
        select(AuditLog, Usuario.nome, Usuario.email)
        .outerjoin(
            Usuario,
            and_(
                Usuario.id == AuditLog.usuario_id,
                Usuario.tenant_id == AuditLog.tenant_id,
            ),
        )
        .where(*filtros)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    linhas = (await db.execute(consulta)).all()
    return AuditoriaListResponse(
        items=[
            AuditoriaItemResponse(
                id=log.id,
                usuario_id=log.usuario_id,
                usuario_nome=usuario_nome,
                usuario_email=usuario_email,
                quando=log.created_at,
                acao=log.acao,
                entidade=log.entidade,
                entidade_id=log.entidade_id,
                dados_antes=_desserializar(log.dados_antes),
                dados_depois=_desserializar(log.dados_depois),
            )
            for log, usuario_nome, usuario_email in linhas
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
