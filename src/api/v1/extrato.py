"""Router de Extrato Bancário — /api/v1/empresas/{empresa_id}/extrato"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.db.session import get_db
from src.domain.extrato.service import ExtratoService
from src.schemas.extrato import (
    ExtratoPendentesResponse,
    ImportacaoResult,
    TransacaoFiltro,
    TransacaoResponse,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/extrato",
    tags=["extrato"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> ExtratoService:
    return ExtratoService(db=db, empresa_id=empresa_id)


@router.post(
    "/importar",
    response_model=ImportacaoResult,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def importar_extrato(
    empresa_id: UUID,
    agencia_id: UUID = Query(..., description="UUID da agência bancária"),
    arquivo: UploadFile = File(..., description="Arquivo OFX ou PDF de extrato bancário"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ImportacaoResult:
    """Importa um arquivo OFX ou PDF e persiste as transações novas (deduplicação automática)."""
    conteudo_bytes = await arquivo.read()
    nome = (arquivo.filename or "").lower()
    svc = _svc(empresa_id, db)

    if nome.endswith(".pdf"):
        from src.domain.extrato.pdf_parser import PDFParseError, parse_pdf
        from src.core.errors import ValidationError as AppValidationError
        try:
            transacoes_pdf = parse_pdf(conteudo_bytes)
        except PDFParseError as e:
            raise AppValidationError(message=f"Arquivo PDF inválido: {e}")
        return await svc.importar_transacoes_raw(transacoes_pdf, agencia_id)
    else:
        # OFX (padrão)
        try:
            conteudo = conteudo_bytes.decode("utf-8")
        except UnicodeDecodeError:
            conteudo = conteudo_bytes.decode("latin-1")
        return await svc.importar_ofx(conteudo, agencia_id)


@router.get("", response_model=ExtratoPendentesResponse)
async def listar_transacoes(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    agencia_id: UUID | None = Query(default=None),
    data_de: str | None = Query(default=None, description="ISO 8601 — ex: 2024-01-01"),
    data_ate: str | None = Query(default=None, description="ISO 8601 — ex: 2024-12-31"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ExtratoPendentesResponse:
    from datetime import datetime

    filtro = TransacaoFiltro(
        status=status,
        agencia_id=agencia_id,
        data_de=datetime.fromisoformat(data_de) if data_de else None,
        data_ate=datetime.fromisoformat(data_ate) if data_ate else None,
    )
    return await _svc(empresa_id, db).listar(filtro, page=page, page_size=page_size)


@router.get("/{transacao_id}", response_model=TransacaoResponse)
async def obter_transacao(
    empresa_id: UUID,
    transacao_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> TransacaoResponse:
    return await _svc(empresa_id, db).obter(transacao_id)
