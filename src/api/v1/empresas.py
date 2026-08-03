"""Router de empresas — /api/v1/empresas"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth, require_csrf
from src.db.session import get_db
from src.domain.empresas.service import EmpresaService
from src.schemas.empresas import (
    CnpjInvalidoListResponse,
    EmpresaCreate,
    EmpresaListResponse,
    EmpresaResponse,
    EmpresaUpdate,
)

router = APIRouter(prefix="/empresas", tags=["empresas"])


def _svc(ctx: AuthContext, db: AsyncSession) -> EmpresaService:
    return EmpresaService(db=db, tenant_id=ctx.tenant_id)


@router.get("", response_model=EmpresaListResponse)
async def listar_empresas(
    page: int = 1,
    page_size: int = 50,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> EmpresaListResponse:
    """Lista todas as empresas do escritório."""
    return await _svc(ctx, db).listar(page=page, page_size=page_size)


# Precisa vir antes de /{empresa_id} — senão o path param captura "cnpj-invalidos"
# e a rota morre num 422 de UUID inválido.
@router.get("/cnpj-invalidos", response_model=CnpjInvalidoListResponse)
async def listar_cnpj_invalidos(
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> CnpjInvalidoListResponse:
    """Empresas cadastradas com CNPJ que não passa na validação de dígito verificador.

    Para essas empresas a exportação de notas fiscais volta vazia sem apresentar erro,
    porque o filtro casa o CNPJ da empresa com o emitente/destinatário da nota.
    """
    return await _svc(ctx, db).listar_cnpj_invalidos()


@router.get("/{empresa_id}", response_model=EmpresaResponse)
async def obter_empresa(
    empresa_id: UUID,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> EmpresaResponse:
    return await _svc(ctx, db).obter(empresa_id)


@router.post("", response_model=EmpresaResponse, status_code=201, dependencies=[Depends(require_csrf)])
async def criar_empresa(
    body: EmpresaCreate,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> EmpresaResponse:
    return await _svc(ctx, db).criar(body)


@router.patch("/{empresa_id}", response_model=EmpresaResponse, dependencies=[Depends(require_csrf)])
async def atualizar_empresa(
    empresa_id: UUID,
    body: EmpresaUpdate,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> EmpresaResponse:
    return await _svc(ctx, db).atualizar(empresa_id, body)


@router.delete("/{empresa_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def desativar_empresa(
    empresa_id: UUID,
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _svc(ctx, db).desativar(empresa_id)
