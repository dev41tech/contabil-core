"""Router da leitura administrativa do trilho de auditoria."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_admin_company_context
from src.core.errors import ValidationError
from src.db.session import get_db
from src.domain.auditoria import listar_auditoria
from src.schemas.auditoria import AuditoriaListResponse
from src.schemas.types import normalizar_competencia

router = APIRouter(
    prefix="/empresas/{empresa_id}/auditoria",
    tags=["auditoria"],
)


@router.get("", response_model=AuditoriaListResponse)
async def consultar_auditoria(
    empresa_id: UUID,
    usuario_id: UUID | None = Query(default=None),
    acao: str | None = Query(default=None, max_length=100),
    entidade: str | None = Query(default=None, max_length=100),
    mes: str | None = Query(default=None, description="Competência no formato AAAA-MM"),
    data_de: date | None = Query(default=None),
    data_ate: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(get_admin_company_context),
    db: AsyncSession = Depends(get_db),
) -> AuditoriaListResponse:
    """Mostra quem alterou o quê na empresa; somente administradores leem."""
    if mes is not None:
        try:
            mes = normalizar_competencia(mes)
        except ValueError as exc:
            raise ValidationError(message=str(exc)) from exc
    if data_de is not None and data_ate is not None and data_de > data_ate:
        raise ValidationError(message="data_de não pode ser posterior a data_ate.")
    return await listar_auditoria(
        db,
        tenant_id=ctx.tenant_id,
        empresa_id=empresa_id,
        usuario_id=usuario_id,
        acao=acao,
        entidade=entidade,
        mes=mes,
        data_de=data_de,
        data_ate=data_ate,
        page=page,
        page_size=page_size,
    )
