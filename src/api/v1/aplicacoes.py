"""Router de Aplicações Financeiras — /api/v1/empresas/{empresa_id}/aplicacoes-financeiras"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.autorizacao import requer
from src.db.session import get_db
from src.domain.aplicacoes.service import AplicacaoFinanceiraService
from src.schemas.aplicacoes import (
    AplicacaoFinanceiraCreate,
    AplicacaoFinanceiraListResponse,
    AplicacaoFinanceiraResponse,
    AplicacaoFinanceiraUpdate,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/aplicacoes-financeiras",
    tags=["aplicacoes-financeiras"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> AplicacaoFinanceiraService:
    return AplicacaoFinanceiraService(db=db, empresa_id=empresa_id)


@router.get("", response_model=AplicacaoFinanceiraListResponse, dependencies=[requer("aplicacoes_financeiras.read")])
async def listar_aplicacoes(
    empresa_id: UUID,
    apenas_ativas: bool = Query(default=False, description="Excluir aplicações encerradas"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AplicacaoFinanceiraListResponse:
    """Lista as aplicações financeiras da empresa, com o total aplicado e atual."""
    return await _svc(empresa_id, db).listar(apenas_ativas=apenas_ativas)


# O GET unitário preserva a forma REST necessária a uma futura tela de detalhe;
# removê-lo pela ausência temporária de UI criaria retrabalho sem ganho real.
@router.get("/{aplicacao_id}", response_model=AplicacaoFinanceiraResponse, dependencies=[requer("aplicacoes_financeiras.read")])
async def obter_aplicacao(
    empresa_id: UUID,
    aplicacao_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AplicacaoFinanceiraResponse:
    return await _svc(empresa_id, db).obter(aplicacao_id)


@router.post(
    "",
    response_model=AplicacaoFinanceiraResponse,
    status_code=201,
    dependencies=[requer("aplicacoes_financeiras.write"), Depends(require_csrf)],
)
async def criar_aplicacao(
    empresa_id: UUID,
    body: AplicacaoFinanceiraCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AplicacaoFinanceiraResponse:
    """Registra uma nova aplicação financeira (CDB, poupança, fundo, tesouro etc.)."""
    return await _svc(empresa_id, db).criar(body)


@router.patch(
    "/{aplicacao_id}",
    response_model=AplicacaoFinanceiraResponse,
    dependencies=[requer("aplicacoes_financeiras.write"), Depends(require_csrf)],
)
async def atualizar_aplicacao(
    empresa_id: UUID,
    aplicacao_id: UUID,
    body: AplicacaoFinanceiraUpdate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> AplicacaoFinanceiraResponse:
    """Atualiza dados do cadastro, o valor atual (rendimento) ou encerra (ativa=false)."""
    return await _svc(empresa_id, db).atualizar(aplicacao_id, body)


@router.delete(
    "/{aplicacao_id}",
    status_code=204,
    dependencies=[requer("aplicacoes_financeiras.write"), Depends(require_csrf)],
)
async def remover_aplicacao(
    empresa_id: UUID,
    aplicacao_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove o cadastro (soft delete). Para encerrar uma aplicação resgatada, use PATCH ativa=false."""
    await _svc(empresa_id, db).remover(aplicacao_id)
