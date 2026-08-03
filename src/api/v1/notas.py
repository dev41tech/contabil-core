"""Router de Notas Fiscais — /api/v1/empresas/{empresa_id}/notas"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.db.session import get_db
from src.domain.notas.service import NotaService
from src.schemas.notas import (
    AssociarTransacaoRequest,
    ImportXmlResponse,
    NotaFiscalCreate,
    NotaFiscalListResponse,
    NotaFiscalResponse,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/notas",
    tags=["notas"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> NotaService:
    return NotaService(db=db, empresa_id=empresa_id)


@router.get("", response_model=NotaFiscalListResponse)
async def listar_notas(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    tipo: str | None = Query(default=None, description="nfe ou nfse"),
    status: str | None = Query(default=None, description="pendente | associada | cancelada"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalListResponse:
    return await _svc(empresa_id, db).listar(
        page=page, page_size=page_size, tipo=tipo, status=status
    )


@router.get("/{nota_id}", response_model=NotaFiscalResponse)
async def obter_nota(
    empresa_id: UUID,
    nota_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalResponse:
    return await _svc(empresa_id, db).obter(nota_id)


@router.post(
    "",
    response_model=NotaFiscalResponse,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def criar_nota(
    empresa_id: UUID,
    body: NotaFiscalCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalResponse:
    return await _svc(empresa_id, db).criar(body)


@router.post(
    "/{nota_id}/associar",
    response_model=NotaFiscalResponse,
    dependencies=[Depends(require_csrf)],
)
async def associar_transacao(
    empresa_id: UUID,
    nota_id: UUID,
    body: AssociarTransacaoRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalResponse:
    """Associa a nota a uma transação bancária importada."""
    return await _svc(empresa_id, db).associar_transacao(nota_id, body)


@router.delete(
    "/{nota_id}/associar",
    response_model=NotaFiscalResponse,
    dependencies=[Depends(require_csrf)],
)
async def desassociar_transacao(
    empresa_id: UUID,
    nota_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalResponse:
    """Remove a associação da nota com uma transação bancária."""
    return await _svc(empresa_id, db).desassociar_transacao(nota_id)


@router.post(
    "/{nota_id}/cancelar",
    response_model=NotaFiscalResponse,
    dependencies=[Depends(require_csrf)],
)
async def cancelar_nota(
    empresa_id: UUID,
    nota_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalResponse:
    return await _svc(empresa_id, db).cancelar(nota_id)


@router.post(
    "/importar-xml",
    response_model=ImportXmlResponse,
    status_code=200,
    dependencies=[Depends(require_csrf)],
)
async def importar_xml_nota(
    empresa_id: UUID,
    arquivo: UploadFile = File(...),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ImportXmlResponse:
    """Importa NF-e ou NFS-e de um arquivo XML ou ZIP contendo múltiplos XMLs."""
    conteudo = await ler_upload_limitado(arquivo)
    nome = arquivo.filename or ""

    if nome.lower().endswith(".zip"):
        resultado = await _svc(empresa_id, db).importar_zip(conteudo)
    else:
        resultado = await _svc(empresa_id, db).importar_xml(conteudo, nome)

    return ImportXmlResponse(
        importadas=resultado.importadas,
        duplicadas=resultado.duplicadas,
        erros=resultado.erros,
        message=f"{resultado.importadas} nota(s) importada(s), {resultado.duplicadas} duplicada(s) ignorada(s).",
    )
