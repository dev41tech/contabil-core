"""Router de Extrato Bancário — /api/v1/empresas/{empresa_id}/extrato"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.api.uploads import ler_upload_limitado
from src.db.models import Job
from src.db.session import get_db
from src.domain.jobs import JobRuntime, executar_importacao_extrato
from src.domain.extrato.service import ExtratoService
from src.schemas.extrato import (
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
    )
    return job


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
