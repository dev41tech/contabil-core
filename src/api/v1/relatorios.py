"""Router Relatórios Financeiros — /api/v1/empresas/{empresa_id}/relatorios"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context
from src.db.session import get_db
from src.domain.relatorios.service import RelatoriosService
from src.schemas.relatorios import BalanceteResponse, DREResponse, LivroCaixaResponse

router = APIRouter(
    prefix="/empresas/{empresa_id}/relatorios",
    tags=["relatorios"],
)


def _svc(
    empresa_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RelatoriosService:
    return RelatoriosService(db=db, empresa_id=empresa_id)


@router.get("/dre", response_model=DREResponse)
async def dre(
    data_de: datetime | None = Query(default=None, description="Início do período"),
    data_ate: datetime | None = Query(default=None, description="Fim do período"),
    svc: RelatoriosService = Depends(_svc),
) -> DREResponse:
    """Demonstração do Resultado do Exercício (DRE)."""
    return await svc.dre(data_de=data_de, data_ate=data_ate)


@router.get("/balancete", response_model=BalanceteResponse)
async def balancete(
    data_de: datetime | None = Query(default=None),
    data_ate: datetime | None = Query(default=None),
    svc: RelatoriosService = Depends(_svc),
) -> BalanceteResponse:
    """Balancete de Verificação com débitos, créditos e saldos por conta."""
    return await svc.balancete(data_de=data_de, data_ate=data_ate)


@router.get("/livro-caixa", response_model=LivroCaixaResponse)
async def livro_caixa(
    data_de: datetime | None = Query(default=None),
    data_ate: datetime | None = Query(default=None),
    svc: RelatoriosService = Depends(_svc),
) -> LivroCaixaResponse:
    """Livro Caixa com movimentação diária e saldo acumulado por agência."""
    return await svc.livro_caixa(data_de=data_de, data_ate=data_ate)
