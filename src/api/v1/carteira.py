"""Router da carteira operacional do escritório."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth
from src.core.errors import ValidationError
from src.core.periodos import competencia_atual
from src.db.session import get_db
from src.domain.carteira import listar_carteira
from src.schemas.carteira import CarteiraResponse
from src.schemas.types import normalizar_competencia

router = APIRouter(prefix="/carteira", tags=["carteira"])


@router.get("", response_model=CarteiraResponse)
async def consultar_carteira(
    mes: str | None = Query(default=None, description="Competência no formato AAAA-MM"),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> CarteiraResponse:
    """Resume o trabalho do mês nas empresas visíveis ao usuário."""
    try:
        competencia = normalizar_competencia(mes or competencia_atual())
    except ValueError as exc:
        raise ValidationError(message=str(exc)) from exc
    return await listar_carteira(
        db,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        role=ctx.role,
        mes=competencia,
    )
