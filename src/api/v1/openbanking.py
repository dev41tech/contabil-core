"""Router Open Banking — /api/v1/empresas/{empresa_id}/openbanking"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.autorizacao import requer
from src.db.session import get_db
from src.domain.openbanking.service import OpenBankingService
from src.schemas.openbanking import (
    ConnectTokenResponse,
    ConexaoListResponse,
    SalvarConexaoRequest,
    SincronizarRequest,
    SincronizarResponse,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/openbanking",
    tags=["openbanking"],
)


def _svc(
    empresa_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> OpenBankingService:
    return OpenBankingService(db=db, empresa_id=empresa_id)


@router.get("/conexoes", response_model=ConexaoListResponse, dependencies=[requer("openbanking.read")])
async def listar_conexoes(
    svc: OpenBankingService = Depends(_svc),
) -> ConexaoListResponse:
    """Lista todas as conexões bancárias da empresa."""
    return await svc.listar()


@router.post(
    "/connect-token",
    response_model=ConnectTokenResponse,
    dependencies=[requer("openbanking.write"), Depends(require_csrf)],
)
async def criar_connect_token(
    svc: OpenBankingService = Depends(_svc),
) -> ConnectTokenResponse:
    """Gera token para abrir o widget de autenticação bancária (Pluggy)."""
    return await svc.criar_connect_token()


@router.post(
    "/conexoes",
    response_model=ConexaoListResponse,
    status_code=201,
    dependencies=[requer("openbanking.write"), Depends(require_csrf)],
)
async def salvar_conexao(
    body: SalvarConexaoRequest,
    svc: OpenBankingService = Depends(_svc),
) -> ConexaoListResponse:
    """Salva todas as contas do item após validar a sessão do widget."""
    return await svc.salvar_conexao(body)


@router.post(
    "/conexoes/{conexao_id}/sincronizar",
    response_model=SincronizarResponse,
    dependencies=[requer("openbanking.execute"), Depends(require_csrf)],
)
async def sincronizar(
    conexao_id: UUID,
    body: SincronizarRequest = SincronizarRequest(),
    svc: OpenBankingService = Depends(_svc),
) -> SincronizarResponse:
    """Sincroniza transações do banco para o sistema (últimos N dias)."""
    return await svc.sincronizar(conexao_id, body)


# Reconectar é uma capacidade real para conexões expiradas; a ausência do botão
# na UI é uma lacuna de exposição, não sinal de que o fluxo esteja morto.
@router.post(
    "/conexoes/{conexao_id}/reconectar",
    response_model=ConnectTokenResponse,
    dependencies=[requer("openbanking.execute"), Depends(require_csrf)],
)
async def reconectar(
    conexao_id: UUID,
    svc: OpenBankingService = Depends(_svc),
) -> ConnectTokenResponse:
    """Gera token para re-autenticar uma conexão expirada."""
    return await svc.criar_reconnect_token(conexao_id)


@router.delete(
    "/conexoes/{conexao_id}",
    status_code=204,
    dependencies=[requer("openbanking.write"), Depends(require_csrf)],
)
async def remover_conexao(
    conexao_id: UUID,
    svc: OpenBankingService = Depends(_svc),
) -> None:
    """Remove (soft delete) uma conexão bancária."""
    await svc.remover(conexao_id)
