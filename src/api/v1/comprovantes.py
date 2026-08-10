"""Router de Comprovantes de Pagamento — /api/v1/empresas/{empresa_id}/comprovantes"""

from __future__ import annotations

import asyncio
import base64
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.core.errors import NotFoundError
from src.db.session import get_db
from src.domain.comprovantes.service import ComprovanteService
from src.schemas.comprovantes import (
    AssociarTransacaoRequest,
    ComprovanteCreate,
    ComprovanteExtracaoResponse,
    ComprovanteListResponse,
    ComprovanteResponse,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/comprovantes",
    tags=["comprovantes"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> ComprovanteService:
    return ComprovanteService(db=db, empresa_id=empresa_id)


@router.get("", response_model=ComprovanteListResponse)
async def listar_comprovantes(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    agencia_id: UUID | None = Query(default=None),
    transacao_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None, description="associado | nao_associado"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ComprovanteListResponse:
    return await _svc(empresa_id, db).listar(
        page=page,
        page_size=page_size,
        agencia_id=agencia_id,
        transacao_id=transacao_id,
        status=status,
    )


@router.get("/{comprovante_id}", response_model=ComprovanteResponse)
async def obter_comprovante(
    empresa_id: UUID,
    comprovante_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ComprovanteResponse:
    return await _svc(empresa_id, db).obter(comprovante_id)


@router.post(
    "",
    response_model=ComprovanteResponse,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def criar_comprovante(
    empresa_id: UUID,
    body: ComprovanteCreate,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ComprovanteResponse:
    return await _svc(empresa_id, db).criar(body)


@router.post(
    "/extrair-pdf",
    response_model=ComprovanteExtracaoResponse,
    dependencies=[Depends(require_csrf)],
)
async def extrair_dados_pdf(
    empresa_id: UUID,
    arquivo: UploadFile = File(..., description="PDF do comprovante de pagamento"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ComprovanteExtracaoResponse:
    """Extrai os campos de um comprovante em PDF para pré-preencher o formulário.

    Não persiste nada — envie os campos revisados via POST /comprovantes normal.
    """
    from src.core.config import get_settings
    from src.core.errors import ValidationError as AppValidationError
    from src.domain.comprovantes.pdf_parser import PDFParseError, parse_pdf

    conteudo = await ler_upload_limitado(arquivo)
    try:
        timeout = get_settings().pdf_parse_timeout_seconds + 5
        extraido = await asyncio.wait_for(
            run_in_threadpool(parse_pdf, conteudo),
            timeout=timeout,
        )
    except TimeoutError:
        raise AppValidationError(
            message="Processamento do PDF excedeu o tempo limite."
        ) from None
    except PDFParseError as e:
        raise AppValidationError(message=f"Arquivo PDF inválido: {e}") from e

    return ComprovanteExtracaoResponse(
        favorecido=extraido.favorecido,
        cpf_cnpj=extraido.cpf_cnpj,
        data_pagamento=extraido.data_pagamento,
        data_vencimento=extraido.data_vencimento,
        valor_documento=extraido.valor_documento,
        valor_pago=extraido.valor_pago,
        juros=extraido.juros,
        multa=extraido.multa,
        desconto=extraido.desconto,
        confianca=extraido.confianca,
    )


@router.post(
    "/{comprovante_id}/associar",
    response_model=ComprovanteResponse,
    dependencies=[Depends(require_csrf)],
)
async def associar_transacao(
    empresa_id: UUID,
    comprovante_id: UUID,
    body: AssociarTransacaoRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ComprovanteResponse:
    """Associa o comprovante a uma transação bancária importada."""
    return await _svc(empresa_id, db).associar_transacao(comprovante_id, body)


@router.delete(
    "/{comprovante_id}/associar",
    response_model=ComprovanteResponse,
    dependencies=[Depends(require_csrf)],
)
async def desassociar_transacao(
    empresa_id: UUID,
    comprovante_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ComprovanteResponse:
    """Remove a associação do comprovante com uma transação bancária."""
    return await _svc(empresa_id, db).desassociar_transacao(comprovante_id)


@router.delete(
    "/{comprovante_id}",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
async def deletar_comprovante(
    empresa_id: UUID,
    comprovante_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _svc(empresa_id, db).deletar(comprovante_id)


@router.get("/{comprovante_id}/arquivo")
async def obter_arquivo(
    empresa_id: UUID,
    comprovante_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Retorna o arquivo (PDF/imagem) do comprovante decodificado do base64."""
    svc = _svc(empresa_id, db)
    comprovante = await svc._get_or_404(comprovante_id)

    if not comprovante.arquivo_base64:
        raise NotFoundError(message="Este comprovante não possui arquivo anexado.")

    arquivo_bytes = base64.b64decode(comprovante.arquivo_base64)

    # Determina o content-type pelo nome do arquivo
    nome = comprovante.arquivo_nome or ""
    nome_lower = nome.lower()
    if nome_lower.endswith(".pdf"):
        media_type = "application/pdf"
    elif nome_lower.endswith(".png"):
        media_type = "image/png"
    elif nome_lower.endswith(".jpg") or nome_lower.endswith(".jpeg"):
        media_type = "image/jpeg"
    else:
        media_type = "application/octet-stream"

    headers = {}
    if nome:
        headers["Content-Disposition"] = f'inline; filename="{nome}"'

    return Response(content=arquivo_bytes, media_type=media_type, headers=headers)
