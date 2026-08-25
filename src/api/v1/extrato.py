"""Router de Extrato Bancário — /api/v1/empresas/{empresa_id}/extrato"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.db.models import Job
from src.db.session import get_db
from src.domain.jobs import JobRuntime, executar_importacao_extrato
from src.domain.extrato.service import ExtratoService
from src.domain.extrato.importacoes import cancelar_importacao, listar_importacoes
from src.schemas.extrato import (
    CancelarImportacaoRequest,
    CancelarImportacaoResponse,
    ImportacaoListResponse,
    ImportacaoResponse,
    ExtratoPendentesResponse,
    TransacaoFiltro,
    TransacaoResponse,
)
from src.schemas.jobs import JobResponse

router = APIRouter(
    prefix="/empresas/{empresa_id}/extrato",
    tags=["extrato"],
)


def _svc(empresa_id: UUID, db: AsyncSession) -> ExtratoService:
    return ExtratoService(db=db, empresa_id=empresa_id)


@router.post(
    "/importar",
    response_model=JobResponse,
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def importar_extrato(
    empresa_id: UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    agencia_id: UUID = Query(..., description="UUID da agência bancária"),
    arquivo: UploadFile = File(..., description="Arquivo OFX ou PDF de extrato bancário"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Enfileira a importação OFX/PDF sem manter a requisição pendurada."""
    conteudo_bytes = await ler_upload_limitado(arquivo)
    nome = (arquivo.filename or "").lower()
    runtime: JobRuntime = getattr(request.app.state, "job_runtime", JobRuntime())
    job = Job(
        empresa_id=empresa_id,
        tipo="extrato_importar",
        status="na_fila",
        criado_por=ctx.user_id,
        heartbeat_em=datetime.now(UTC),
    )
    db.add(job)
    await db.flush()
    if runtime.commit:
        await db.commit()
    background_tasks.add_task(
        executar_importacao_extrato,
        job.id,
        empresa_id,
        agencia_id,
        nome,
        conteudo_bytes,
        runtime,
        ctx.user_id,
    )
    return job


@router.get("", response_model=ExtratoPendentesResponse)
async def listar_transacoes(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    agencia_id: UUID | None = Query(default=None),
    # `date` e não `str`: o FastAPI valida e converte, e o intervalo é inclusivo
    # nos dois extremos — informar 2026-01-31 traz os lançamentos daquele dia.
    data_de: date | None = Query(default=None, description="AAAA-MM-DD — ex: 2026-01-01"),
    data_ate: date | None = Query(default=None, description="AAAA-MM-DD — ex: 2026-12-31"),
    historico: str | None = Query(default=None, description="Busca parcial no histórico"),
    dc: str | None = Query(default=None, pattern="^[DC]$", description="D ou C"),
    valor_min: Decimal | None = Query(default=None, ge=0, description="Valor mínimo"),
    valor_max: Decimal | None = Query(default=None, ge=0, description="Valor máximo"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ExtratoPendentesResponse:
    filtro = TransacaoFiltro(
        status=status,
        agencia_id=agencia_id,
        data_de=data_de,
        data_ate=data_ate,
        historico=historico,
        dc=dc,
        valor_min=valor_min,
        valor_max=valor_max,
    )
    return await _svc(empresa_id, db).listar(filtro, page=page, page_size=page_size)


# NOTA: as rotas de /importacoes precisam vir ANTES de /{transacao_id}. O
# FastAPI casa na ordem de registro, e depois da rota curinga um GET em
# /extrato/importacoes tentaria ler "importacoes" como UUID e responderia 422.
@router.get("/importacoes", response_model=ImportacaoListResponse)
async def listar_importacoes_endpoint(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    agencia_id: UUID | None = Query(default=None),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> ImportacaoListResponse:
    """Uploads de extrato da empresa, do mais recente para o mais antigo."""
    itens, ativas, total = await listar_importacoes(
        db, empresa_id=empresa_id, agencia_id=agencia_id, page=page, page_size=page_size
    )
    resposta = []
    for importacao, quantidade in zip(itens, ativas):
        item = ImportacaoResponse.model_validate(importacao)
        item.transacoes_ativas = quantidade
        resposta.append(item)
    return ImportacaoListResponse(
        items=resposta, total=total, page=page, page_size=page_size
    )


@router.post(
    "/importacoes/{importacao_id}/cancelar",
    response_model=CancelarImportacaoResponse,
    dependencies=[Depends(require_csrf)],
)
async def cancelar_importacao_endpoint(
    empresa_id: UUID,
    importacao_id: UUID,
    body: CancelarImportacaoRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> CancelarImportacaoResponse:
    """Desfaz um upload inteiro — transações e lançamentos que vieram dele.

    Transação já contabilizada tem o lançamento cancelado ANTES de ser removida:
    o inverso deixaria partidas órfãs no razão, sem transação para explicá-las.
    """
    resultado = await cancelar_importacao(
        db,
        empresa_id=empresa_id,
        importacao_id=importacao_id,
        motivo=body.motivo,
        usuario_id=ctx.user_id,
    )
    return CancelarImportacaoResponse(
        importacao_id=resultado.importacao_id,
        transacoes_removidas=resultado.transacoes_removidas,
        lancamentos_cancelados=resultado.lancamentos_cancelados,
    )


@router.get("/{transacao_id}", response_model=TransacaoResponse)
async def obter_transacao(
    empresa_id: UUID,
    transacao_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> TransacaoResponse:
    return await _svc(empresa_id, db).obter(transacao_id)
