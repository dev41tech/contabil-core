"""Router de Contrapartes — /api/v1/empresas/{empresa_id}/contrapartes"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.autorizacao import requer
from src.db.session import get_db
from src.domain.contrapartes.service import ContraparteService
from src.schemas.contrapartes import (
    ContraparteCreate,
    ContraparteListResponse,
    ContraparteResponse,
    ContraparteUpdate,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/contrapartes",
    tags=["contrapartes"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> ContraparteService:
    return ContraparteService(db=db, empresa_id=empresa_id)


@router.get("", response_model=ContraparteListResponse, dependencies=[requer("contrapartes.read")])
async def listar_contrapartes(
    empresa_id: UUID,
    termo: str | None = Query(default=None, description="Busca por razão social, nome fantasia ou documento"),
    tipo: str | None = Query(default=None, description="fornecedor, cliente ou ambos"),
    apenas_ativas: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ContraparteListResponse:
    return await _svc(empresa_id, db).listar(
        termo=termo, tipo=tipo, apenas_ativas=apenas_ativas, page=page, page_size=page_size
    )


@router.get("/{contraparte_id}", response_model=ContraparteResponse, dependencies=[requer("contrapartes.read")])
async def obter_contraparte(
    empresa_id: UUID,
    contraparte_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ContraparteResponse:
    return await _svc(empresa_id, db).obter(contraparte_id)


@router.post(
    "",
    response_model=ContraparteResponse,
    status_code=201,
    dependencies=[requer("contrapartes.write"), Depends(require_csrf)],
)
async def criar_contraparte(
    empresa_id: UUID,
    body: ContraparteCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ContraparteResponse:
    """Cadastra um fornecedor/cliente identificado por CPF/CNPJ e sua conta contábil padrão."""
    return await _svc(empresa_id, db).criar(body)


@router.patch(
    "/{contraparte_id}",
    response_model=ContraparteResponse,
    dependencies=[requer("contrapartes.write"), Depends(require_csrf)],
)
async def atualizar_contraparte(
    empresa_id: UUID,
    contraparte_id: UUID,
    body: ContraparteUpdate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ContraparteResponse:
    return await _svc(empresa_id, db).atualizar(contraparte_id, body)


@router.delete(
    "/{contraparte_id}",
    status_code=204,
    dependencies=[requer("contrapartes.write"), Depends(require_csrf)],
)
async def remover_contraparte(
    empresa_id: UUID,
    contraparte_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove o cadastro (soft delete). Para desativar sem apagar, use PATCH ativa=false."""
    await _svc(empresa_id, db).remover(contraparte_id)
