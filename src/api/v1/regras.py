"""Router de Regras de Categorização — /api/v1/empresas/{empresa_id}/regras"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.db.session import get_db
from src.domain.regras.service import RegraService
from src.schemas.regras import (
    RegraCreate,
    RegraListResponse,
    RegraResponse,
    RegraUpdate,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/regras",
    tags=["regras"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> RegraService:
    return RegraService(db=db, empresa_id=empresa_id)


@router.get("", response_model=RegraListResponse)
async def listar_regras(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    apenas_ativas: bool = Query(default=False),
    agencia_id: UUID | None = Query(default=None),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RegraListResponse:
    return await _svc(empresa_id, db).listar(
        page=page,
        page_size=page_size,
        apenas_ativas=apenas_ativas,
        agencia_id=agencia_id,
    )


@router.get("/{regra_id}", response_model=RegraResponse)
async def obter_regra(
    empresa_id: UUID,
    regra_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RegraResponse:
    return await _svc(empresa_id, db).obter(regra_id)


@router.post(
    "",
    response_model=RegraResponse,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def criar_regra(
    empresa_id: UUID,
    body: RegraCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RegraResponse:
    return await _svc(empresa_id, db).criar(body)


@router.patch(
    "/{regra_id}",
    response_model=RegraResponse,
    dependencies=[Depends(require_csrf)],
)
async def atualizar_regra(
    empresa_id: UUID,
    regra_id: UUID,
    body: RegraUpdate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> RegraResponse:
    return await _svc(empresa_id, db).atualizar(regra_id, body)


@router.delete(
    "/{regra_id}",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
async def desativar_regra(
    empresa_id: UUID,
    regra_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _svc(empresa_id, db).desativar(regra_id)
