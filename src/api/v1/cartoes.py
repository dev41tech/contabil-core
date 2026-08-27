"""Router de Cartão de Crédito — /api/v1/empresas/{empresa_id}/cartoes"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, UploadFile, File

from src.api.uploads import ler_upload_limitado
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.autorizacao import requer
from src.db.session import get_db
from src.domain.cartoes.service import CartaoService
from src.schemas.cartoes import (
    AssociarTransacaoFaturaRequest,
    CartaoCreate,
    CartaoListResponse,
    CartaoResponse,
    CartaoUpdate,
    FaturaCreate,
    FaturaListResponse,
    FaturaResponse,
    FaturaUpdate,
    ImportCSVResponse,
    LancamentoBulkImport,
    LancamentoCreate,
    LancamentoListResponse,
    LancamentoResponse,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/cartoes",
    tags=["cartoes"],
)


def _svc(
    empresa_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> CartaoService:
    return CartaoService(db=db, empresa_id=empresa_id)


# ── Cartões ───────────────────────────────────────────────────────────────────

@router.get("", response_model=CartaoListResponse, dependencies=[requer("cartoes.read")])
async def listar_cartoes(
    svc: CartaoService = Depends(_svc),
) -> CartaoListResponse:
    return await svc.listar_cartoes()


@router.post("", response_model=CartaoResponse, status_code=201,
             dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def criar_cartao(
    body: CartaoCreate,
    svc: CartaoService = Depends(_svc),
) -> CartaoResponse:
    return await svc.criar_cartao(body)


@router.patch("/{cartao_id}", response_model=CartaoResponse,
              dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def atualizar_cartao(
    cartao_id: UUID,
    body: CartaoUpdate,
    svc: CartaoService = Depends(_svc),
) -> CartaoResponse:
    return await svc.atualizar_cartao(cartao_id, body)


@router.delete("/{cartao_id}", status_code=204, dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def remover_cartao(
    cartao_id: UUID,
    svc: CartaoService = Depends(_svc),
) -> None:
    await svc.remover_cartao(cartao_id)


# ── Faturas ───────────────────────────────────────────────────────────────────

@router.get("/{cartao_id}/faturas", response_model=FaturaListResponse, dependencies=[requer("cartoes.read")])
async def listar_faturas(
    cartao_id: UUID,
    svc: CartaoService = Depends(_svc),
) -> FaturaListResponse:
    return await svc.listar_faturas(cartao_id)


@router.post("/{cartao_id}/faturas", response_model=FaturaResponse, status_code=201,
             dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def criar_fatura(
    cartao_id: UUID,
    body: FaturaCreate,
    svc: CartaoService = Depends(_svc),
) -> FaturaResponse:
    return await svc.criar_fatura(cartao_id, body)


@router.patch("/{cartao_id}/faturas/{fatura_id}", response_model=FaturaResponse,
              dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def atualizar_fatura(
    cartao_id: UUID,
    fatura_id: UUID,
    body: FaturaUpdate,
    svc: CartaoService = Depends(_svc),
) -> FaturaResponse:
    return await svc.atualizar_fatura(cartao_id, fatura_id, body)


@router.post("/{cartao_id}/faturas/{fatura_id}/associar-transacao",
             response_model=FaturaResponse, dependencies=[requer("cartoes.execute"), Depends(require_csrf)])
async def associar_transacao(
    cartao_id: UUID,
    fatura_id: UUID,
    body: AssociarTransacaoFaturaRequest,
    svc: CartaoService = Depends(_svc),
) -> FaturaResponse:
    """Associa a transação bancária que pagou a fatura e marca como 'paga'."""
    return await svc.associar_transacao(cartao_id, fatura_id, body)


@router.delete("/{cartao_id}/faturas/{fatura_id}/associar-transacao",
               response_model=FaturaResponse, dependencies=[requer("cartoes.execute"), Depends(require_csrf)])
async def desassociar_transacao(
    cartao_id: UUID,
    fatura_id: UUID,
    svc: CartaoService = Depends(_svc),
) -> FaturaResponse:
    """Remove a associação com a transação bancária e reverte para 'fechada'."""
    return await svc.desassociar_transacao(cartao_id, fatura_id)


# ── Lançamentos ───────────────────────────────────────────────────────────────

@router.get("/{cartao_id}/faturas/{fatura_id}/lancamentos",
            response_model=LancamentoListResponse,
    dependencies=[requer("cartoes.read")],
)
async def listar_lancamentos(
    cartao_id: UUID,
    fatura_id: UUID,
    svc: CartaoService = Depends(_svc),
) -> LancamentoListResponse:
    return await svc.listar_lancamentos(cartao_id, fatura_id)


@router.post("/{cartao_id}/faturas/{fatura_id}/lancamentos",
             response_model=LancamentoResponse, status_code=201,
             dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def adicionar_lancamento(
    cartao_id: UUID,
    fatura_id: UUID,
    body: LancamentoCreate,
    svc: CartaoService = Depends(_svc),
) -> LancamentoResponse:
    return await svc.adicionar_lancamento(cartao_id, fatura_id, body)


@router.post("/{cartao_id}/faturas/{fatura_id}/lancamentos/importar-csv",
             response_model=ImportCSVResponse, dependencies=[requer("cartoes.execute"), Depends(require_csrf)])
async def importar_csv(
    cartao_id: UUID,
    fatura_id: UUID,
    arquivo: UploadFile = File(..., description="CSV com colunas: data_compra, descricao, valor"),
    svc: CartaoService = Depends(_svc),
) -> ImportCSVResponse:
    """Importa lançamentos via CSV. Colunas: data_compra, descricao, valor."""
    conteudo = await ler_upload_limitado(arquivo)
    return await svc.importar_csv(cartao_id, fatura_id, conteudo)


@router.post("/{cartao_id}/faturas/{fatura_id}/lancamentos/importar-pdf",
             response_model=ImportCSVResponse, dependencies=[requer("cartoes.execute"), Depends(require_csrf)])
async def importar_pdf(
    cartao_id: UUID,
    fatura_id: UUID,
    arquivo: UploadFile = File(..., description="PDF da fatura do cartão"),
    svc: CartaoService = Depends(_svc),
) -> ImportCSVResponse:
    """Importa lançamentos a partir do PDF da fatura (extração automática)."""
    import asyncio

    from starlette.concurrency import run_in_threadpool

    from src.core.config import get_settings
    from src.core.errors import ValidationError as AppValidationError
    from src.domain.cartoes.pdf_parser import PDFParseError, parse_pdf

    conteudo = await ler_upload_limitado(arquivo)
    try:
        timeout = get_settings().pdf_parse_timeout_seconds + 5
        lancamentos = await asyncio.wait_for(
            run_in_threadpool(parse_pdf, conteudo),
            timeout=timeout,
        )
    except TimeoutError:
        raise AppValidationError(
            message="Processamento do PDF excedeu o tempo limite."
        ) from None
    except PDFParseError as e:
        raise AppValidationError(message=f"Arquivo PDF inválido: {e}") from e

    return await svc.importar_lancamentos_pdf(cartao_id, fatura_id, lancamentos)


@router.delete("/{cartao_id}/faturas/{fatura_id}/lancamentos/{lancamento_id}",
               status_code=204, dependencies=[requer("cartoes.write"), Depends(require_csrf)])
async def remover_lancamento(
    cartao_id: UUID,
    fatura_id: UUID,
    lancamento_id: UUID,
    svc: CartaoService = Depends(_svc),
) -> None:
    await svc.remover_lancamento(cartao_id, fatura_id, lancamento_id)
