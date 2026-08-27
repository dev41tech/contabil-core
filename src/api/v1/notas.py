"""Router de Notas Fiscais — /api/v1/empresas/{empresa_id}/notas"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.api.autorizacao import requer
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


@router.get("", response_model=NotaFiscalListResponse, dependencies=[requer("notas.read")])
async def listar_notas(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    tipo: str | None = Query(default=None, description="nfe ou nfse"),
    status: str | None = Query(default=None, description="pendente | associada | cancelada"),
    numero: str | None = Query(default=None, description="Busca parcial pelo número da nota"),
    chave_acesso: str | None = Query(
        default=None, description="Busca parcial pela chave de acesso (44 dígitos)"
    ),
    cnpj: str | None = Query(
        default=None,
        description="CNPJ do emitente ou destinatário — completo (exato) ou trecho (parcial)",
    ),
    emitente: str | None = Query(default=None, description="Busca parcial pelo nome do emitente"),
    data_de: datetime | None = Query(default=None, description="Data de emissão inicial"),
    data_ate: datetime | None = Query(default=None, description="Data de emissão final"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NotaFiscalListResponse:
    return await _svc(empresa_id, db).listar(
        page=page,
        page_size=page_size,
        tipo=tipo,
        status=status,
        numero=numero,
        chave_acesso=chave_acesso,
        cnpj=cnpj,
        emitente=emitente,
        data_de=data_de,
        data_ate=data_ate,
    )


# O GET unitário é a forma REST natural e viabiliza uma futura tela de detalhe
# sem obrigar o cliente a localizar a nota dentro de uma página da listagem.
@router.get("/{nota_id}", response_model=NotaFiscalResponse, dependencies=[requer("notas.read")])
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
    dependencies=[requer("notas.write"), Depends(require_csrf)],
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
    dependencies=[requer("notas.write"), Depends(require_csrf)],
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
    dependencies=[requer("notas.write"), Depends(require_csrf)],
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
    dependencies=[requer("notas.write"), Depends(require_csrf)],
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
    dependencies=[requer("notas.write"), Depends(require_csrf)],
)
async def importar_xml_nota(
    empresa_id: UUID,
    arquivo: UploadFile = File(
        ..., description="XML, ZIP (múltiplos XMLs), PDF ou imagem (PNG/JPG) de nota fiscal"
    ),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ImportXmlResponse:
    """Importa NF-e/NFS-e de um XML, ZIP (múltiplos XMLs), PDF ou imagem (DANFe).

    Nota importada via PDF/imagem não tem assinatura digital verificável — fica
    marcada com `origem="ocr"` em vez de `origem="xml_assinado"` (ver
    `NotaService.importar_visual`).
    """
    conteudo = await ler_upload_limitado(arquivo)
    nome = arquivo.filename or ""
    nome_lower = nome.lower()

    svc = _svc(empresa_id, db)
    if nome_lower.endswith(".zip"):
        resultado = await svc.importar_zip(conteudo)
    elif nome_lower.endswith((".pdf", ".png", ".jpg", ".jpeg")):
        extensao = "." + nome_lower.rsplit(".", 1)[-1]
        resultado = await svc.importar_visual(conteudo, nome, extensao)
    else:
        resultado = await svc.importar_xml(conteudo, nome)

    return ImportXmlResponse(
        importadas=resultado.importadas,
        duplicadas=resultado.duplicadas,
        erros=resultado.erros,
        message=f"{resultado.importadas} nota(s) importada(s), {resultado.duplicadas} duplicada(s) ignorada(s).",
    )
