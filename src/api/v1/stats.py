"""Router de Estatísticas — /api/v1/empresas/{empresa_id}/stats"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context
from src.api.autorizacao import requer
from src.db.session import get_db
from src.domain.stats.service import StatsService
from src.schemas.stats import StatsResponse

router = APIRouter(
    prefix="/empresas/{empresa_id}/stats",
    tags=["stats"],
)


@router.get("", response_model=StatsResponse, dependencies=[requer("stats.read")])
async def obter_stats(
    empresa_id: UUID,
    meses: int = Query(default=12, ge=1, le=24, description="Quantos meses históricos"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> StatsResponse:
    """Retorna resumo e série mensal para os gráficos do Dashboard."""
    return await StatsService(db=db, empresa_id=empresa_id).obter(meses=meses)
