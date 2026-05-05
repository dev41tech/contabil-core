"""Router de Permissões por empresa — /api/v1/empresas/{empresa_id}/permissoes"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_admin, require_csrf
from src.db.session import get_db
from src.domain.permissoes.service import PermissaoService
from src.schemas.permissoes import (
    PermissaoCreate,
    PermissaoListResponse,
    PermissaoResponse,
    PermissaoUpdate,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/permissoes",
    tags=["permissoes"],
)


def _svc(
    empresa_id: UUID,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PermissaoService:
    return PermissaoService(db=db, empresa_id=empresa_id, tenant_id=ctx.tenant_id)


@router.get("", response_model=PermissaoListResponse)
async def listar_permissoes(
    svc: PermissaoService = Depends(_svc),
) -> PermissaoListResponse:
    """Lista todos os usuários com acesso à empresa."""
    return await svc.listar()


@router.post(
    "",
    response_model=PermissaoResponse,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def conceder_acesso(
    body: PermissaoCreate,
    svc: PermissaoService = Depends(_svc),
) -> PermissaoResponse:
    """Concede acesso de um usuário a esta empresa."""
    return await svc.conceder(body)


@router.patch(
    "/{usuario_id}",
    response_model=PermissaoResponse,
    dependencies=[Depends(require_csrf)],
)
async def atualizar_modulos(
    usuario_id: UUID,
    body: PermissaoUpdate,
    svc: PermissaoService = Depends(_svc),
) -> PermissaoResponse:
    """Atualiza os módulos liberados para o usuário nesta empresa."""
    return await svc.atualizar(usuario_id, body)


@router.delete(
    "/{usuario_id}",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
async def revogar_acesso(
    usuario_id: UUID,
    svc: PermissaoService = Depends(_svc),
) -> None:
    """Revoga o acesso do usuário a esta empresa."""
    await svc.revogar(usuario_id)
