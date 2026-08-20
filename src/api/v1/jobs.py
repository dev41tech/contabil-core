"""Consulta do histórico e estado dos jobs persistentes."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context
from src.db.models import Job, Permissao
from src.db.session import get_db
from src.schemas.jobs import JobListResponse, JobResponse

router = APIRouter(prefix="/empresas/{empresa_id}/jobs", tags=["jobs"])


async def _tipos_permitidos(
    db: AsyncSession, empresa_id: UUID, ctx: AuthContext
) -> set[str]:
    """Traduz permissões dos módulos de origem nos tipos visíveis de job."""
    if ctx.role == "admin":
        return {"neo_processar", "extrato_importar"}
    permissao = (
        await db.execute(
            select(Permissao).where(
                Permissao.usuario_id == ctx.user_id,
                Permissao.empresa_id == empresa_id,
            )
        )
    ).scalar_one()
    modulos = {item.strip() for item in permissao.modulos.split(",") if item.strip()}
    if "*" in modulos or "jobs" in modulos:
        return {"neo_processar", "extrato_importar"}
    permitidos: set[str] = set()
    if "neo" in modulos:
        permitidos.add("neo_processar")
    if "extrato" in modulos:
        permitidos.add("extrato_importar")
    return permitidos


@router.get("", response_model=JobListResponse)
async def listar_jobs(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    tipo: Literal["neo_processar", "extrato_importar"] | None = Query(default=None),
    status: Literal[
        "na_fila", "processando", "concluido", "concluido_com_alertas", "falhou"
    ] | None = Query(default=None),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    """Lista apenas jobs da empresa autorizada, do mais recente ao mais antigo."""
    permitidos = await _tipos_permitidos(db, empresa_id, ctx)
    filtros = [Job.empresa_id == empresa_id, Job.tipo.in_(permitidos)]
    if tipo is not None:
        filtros.append(Job.tipo == tipo)
    if status is not None:
        filtros.append(Job.status == status)
    total = (await db.execute(select(func.count()).select_from(Job).where(*filtros))).scalar_one()
    items = (
        await db.execute(
            select(Job).where(*filtros).order_by(Job.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return JobListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/{job_id}", response_model=JobResponse)
async def obter_job(
    empresa_id: UUID,
    job_id: UUID,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> Job:
    """Obtém um job sem permitir que seu UUID atravesse empresas."""
    permitidos = await _tipos_permitidos(db, empresa_id, ctx)
    job = (
        await db.execute(
            select(Job).where(
                Job.id == job_id,
                Job.empresa_id == empresa_id,
                Job.tipo.in_(permitidos),
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return job
