"""Router de Registros Contábeis — /api/v1/empresas/{empresa_id}/contabil"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context
from src.db.session import get_db
from src.domain.contabil.service import ContabilService
from src.schemas.contabil import RegistroContabilListResponse, RegistroContabilResponse

router = APIRouter(
    prefix="/empresas/{empresa_id}/contabil",
    tags=["contabil"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> ContabilService:
    return ContabilService(db=db, empresa_id=empresa_id)


@router.get("", response_model=RegistroContabilListResponse)
async def listar_registros(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    conta_id: UUID | None = Query(default=None),
    agencia_id: UUID | None = Query(default=None),
    dc: str | None = Query(default=None, description="D ou C"),
    data_de: date | datetime | None = Query(default=None, description="ISO 8601"),
    data_ate: date | datetime | None = Query(default=None, description="ISO 8601"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RegistroContabilListResponse:
    """Lista registros contábeis gerados pelo NEO ou inseridos manualmente."""
    return await _svc(empresa_id, db).listar(
        page=page,
        page_size=page_size,
        conta_id=conta_id,
        agencia_id=agencia_id,
        dc=dc,
        data_de=data_de,
        data_ate=data_ate,
    )


# O GET unitário mantém o contrato REST pronto para uma tela de detalhe e custa
# pouco comparado a obrigar todo consumidor futuro a vasculhar a listagem.
@router.get("/{registro_id}", response_model=RegistroContabilResponse)
async def obter_registro(
    empresa_id: UUID,
    registro_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RegistroContabilResponse:
    return await _svc(empresa_id, db).obter(registro_id)
