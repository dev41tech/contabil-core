"""Router de agências bancárias — /api/v1/empresas/{empresa_id}/agencias

As agências são sempre filhas de uma empresa. A URL reflete essa hierarquia.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.autorizacao import requer
from src.db.session import get_db
from src.domain.agencias.service import AgenciaService
from src.schemas.agencias import (
    AgenciaCreate,
    AgenciaListResponse,
    AgenciaResponse,
    AgenciaUpdate,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/agencias",
    tags=["agencias"],
)


def _svc(empresa_id: UUID, ctx: AuthContext, db: AsyncSession) -> AgenciaService:
    return AgenciaService(db=db, empresa_id=empresa_id, tenant_id=ctx.tenant_id)


@router.get("", response_model=AgenciaListResponse, dependencies=[requer("agencias.read")])
async def listar_agencias(
    empresa_id: UUID,
    apenas_ativas: bool = Query(default=False, description="Filtrar apenas contas ativas"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AgenciaListResponse:
    """Lista todas as contas bancárias da empresa."""
    return await _svc(empresa_id, ctx, db).listar(apenas_ativas=apenas_ativas)


# O recurso unitário é o contrato natural para detalhe/edição futura; a lacuna
# atual é de tela, e não uma indicação de que a capacidade deva ser podada.
@router.get("/{agencia_id}", response_model=AgenciaResponse, dependencies=[requer("agencias.read")])
async def obter_agencia(
    empresa_id: UUID,
    agencia_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AgenciaResponse:
    return await _svc(empresa_id, ctx, db).obter(agencia_id)


@router.post(
    "",
    response_model=AgenciaResponse,
    status_code=201,
    dependencies=[requer("agencias.write"), Depends(require_csrf)],
)
async def criar_agencia(
    empresa_id: UUID,
    body: AgenciaCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AgenciaResponse:
    """Cadastra uma nova conta bancária para a empresa."""
    return await _svc(empresa_id, ctx, db).criar(body)


@router.patch(
    "/{agencia_id}",
    response_model=AgenciaResponse,
    dependencies=[requer("agencias.write"), Depends(require_csrf)],
)
async def atualizar_agencia(
    empresa_id: UUID,
    agencia_id: UUID,
    body: AgenciaUpdate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AgenciaResponse:
    """Atualiza dados da conta bancária. Campos omitidos não são alterados."""
    return await _svc(empresa_id, ctx, db).atualizar(agencia_id, body)


@router.delete(
    "/{agencia_id}",
    status_code=204,
    dependencies=[requer("agencias.write"), Depends(require_csrf)],
)
async def desativar_agencia(
    empresa_id: UUID,
    agencia_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Desativa a conta bancária. O histórico de transações é preservado."""
    await _svc(empresa_id, ctx, db).desativar(agencia_id)
