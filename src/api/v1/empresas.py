"""Router de empresas — /api/v1/empresas"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_admin, require_auth, require_csrf
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
    return EmpresaService(
        db=db,
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        role=ctx.role,
    )


@router.get("", response_model=EmpresaListResponse)
async def listar_empresas(
    page: int = Query(default=1, ge=1),
    # Sem `le`, um escritório com muitas empresas cadastradas ficava sujeito a
    # "faltam empresas na lista" sempre que um cliente do front esquecesse de
    # paginar — foi exatamente o que aconteceu: o seletor global buscava sem
    # page_size, caía no default de 50 e as empresas seguintes ao 50º lugar
    # (em ordem alfabética) nunca apareciam em nenhuma tela. O limite aqui só
    # deixa o contrato explícito; a correção real é o front paginar de verdade.
    page_size: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> EmpresaListResponse:
    """Lista todas as empresas do escritório."""
    return await _svc(ctx, db).listar(page=page, page_size=page_size)


# Sustenta a tarefa aberta de levantar com o escritório os 70 CNPJs inválidos;
# não ter tela ainda não torna dispensável essa consulta operacional.
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


# Consultar, editar e excluir uma empresa são lacunas de tela já mapeadas. As
# rotas ficam como contrato do domínio enquanto a UI administrativa não chega.
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
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> EmpresaResponse:
    return await _svc(ctx, db).criar(body)


@router.patch("/{empresa_id}", response_model=EmpresaResponse, dependencies=[Depends(require_csrf)])
async def atualizar_empresa(
    empresa_id: UUID,
    body: EmpresaUpdate,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> EmpresaResponse:
    return await _svc(ctx, db).atualizar(empresa_id, body)


@router.delete("/{empresa_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def desativar_empresa(
    empresa_id: UUID,
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _svc(ctx, db).desativar(empresa_id)
