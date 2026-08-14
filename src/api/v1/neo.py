"""Router do NEO (motor de matching automático) — /api/v1/empresas/{empresa_id}/neo"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.db.models import NeoDecisao, PlanoConta, Regra, Transacao
from src.db.session import get_db
from src.domain.auditoria import registrar_auditoria
from src.domain.neo.engine import NeoEngine
from src.schemas.neo import (
    NeoAssociarManualRequest,
    NeoDecisaoListResponse,
    NeoDecisaoResponse,
    NeoProcessarRequest,
    NeoResultado,
)

router = APIRouter(
    prefix="/empresas/{empresa_id}/neo",
    tags=["neo"],
)


@router.post(
    "/processar",
    response_model=NeoResultado,
    dependencies=[Depends(require_csrf)],
)
async def processar(
    empresa_id: UUID,
    body: NeoProcessarRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoResultado:
    """Executa o motor de matching nas transações pendentes.

    Idempotente — transações já processadas são ignoradas.
    Se agencia_id for informado, processa apenas aquela agência.
    Se mes for informado (AAAA-MM), processa apenas transações daquele mês.
    """
    engine = NeoEngine(db=db, empresa_id=empresa_id)
    return await engine.processar(agencia_id=body.agencia_id, mes=body.mes)


@router.get("/decisoes", response_model=NeoDecisaoListResponse)
async def listar_decisoes(
    empresa_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    resultado: str | None = Query(default=None, description="associada | sem_regra | erro"),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoDecisaoListResponse:
    """Lista o log de decisões do NEO para a empresa."""
    q = select(NeoDecisao).where(NeoDecisao.empresa_id == empresa_id)
    if resultado:
        q = q.where(NeoDecisao.resultado == resultado)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    rows = (
        await db.execute(
            q.order_by(NeoDecisao.processado_em.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    # Collect IDs for batch fetch (avoids N+1)
    transacao_ids = [r.transacao_id for r in rows]
    regra_ids = [r.regra_id for r in rows if r.regra_id]

    transacoes_map: dict = {}
    if transacao_ids:
        t_rows = (await db.execute(
            select(Transacao).where(Transacao.id.in_(transacao_ids))
        )).scalars().all()
        transacoes_map = {t.id: t for t in t_rows}

    regras_map: dict = {}
    if regra_ids:
        reg_rows = (await db.execute(
            select(Regra).where(Regra.id.in_(regra_ids))
        )).scalars().all()
        regras_map = {reg.id: reg for reg in reg_rows}

    items = []
    for r in rows:
        t = transacoes_map.get(r.transacao_id)
        reg = regras_map.get(r.regra_id) if r.regra_id else None

        item = NeoDecisaoResponse.model_validate(r)
        item.transacao_descricao = t.historico if t else None
        item.transacao_valor = t.valor if t else None
        item.transacao_dc = t.dc if t else None
        item.agencia_id = t.agencia_id if t else None
        item.regra_descricao = reg.descricao if reg else None
        items.append(item)

    return NeoDecisaoListResponse(items=items, total=total)


@router.post(
    "/decisoes/{decisao_id}/associar-manual",
    response_model=NeoDecisaoResponse,
    dependencies=[Depends(require_csrf)],
)
async def associar_manual(
    empresa_id: UUID,
    decisao_id: UUID,
    body: NeoAssociarManualRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoDecisaoResponse:
    """Associa manualmente uma transação sem regra a uma conta contábil.

    Cria as duas partidas do lançamento com estrategia='manual', marca a
    decisão como 'associada' e atualiza o status da transação para 'processada'.
    """
    # Busca e valida a decisão
    decisao_result = await db.execute(
        select(NeoDecisao).where(
            NeoDecisao.id == decisao_id,
            NeoDecisao.empresa_id == empresa_id,
        )
    )
    decisao = decisao_result.scalar_one_or_none()
    if not decisao:
        raise HTTPException(status_code=404, detail="Decisão não encontrada.")
    if decisao.resultado != "sem_regra":
        raise HTTPException(
            status_code=422,
            detail="Apenas decisões com resultado 'sem_regra' podem ser associadas manualmente.",
        )

    # Bloqueia a transação para serializar esta associação com o processamento automático.
    t_result = await db.execute(
        select(Transacao)
        .where(
            Transacao.id == decisao.transacao_id,
            Transacao.empresa_id == empresa_id,
        )
        .with_for_update()
    )
    transacao = t_result.scalar_one_or_none()
    if not transacao:
        raise HTTPException(status_code=404, detail="Transação não encontrada.")
    if transacao.status != "pendente":
        raise HTTPException(
            status_code=422,
            detail="A transação já foi contabilizada ou não está mais pendente.",
        )

    conta = (
        await db.execute(
            select(PlanoConta).where(
                PlanoConta.id == body.conta_id,
                PlanoConta.empresa_id == empresa_id,
                PlanoConta.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conta:
        raise HTTPException(
            status_code=422,
            detail="Conta contábil não encontrada nesta empresa.",
        )

    antes = {
        "resultado": decisao.resultado,
        "estrategia": decisao.estrategia,
        "motivo": decisao.motivo,
        "transacao_status": transacao.status,
    }
    engine = NeoEngine(db=db, empresa_id=empresa_id)
    await engine.registrar_partidas_manuais(transacao, conta.id, body.descricao)

    # Atualiza a decisão
    decisao.resultado = "associada"
    decisao.estrategia = "manual"
    decisao.motivo = f"Associação manual: {body.descricao}"

    # Atualiza o status da transação
    transacao.status = "processada"

    await registrar_auditoria(
        db,
        tenant_id=ctx.tenant_id,
        usuario_id=ctx.user_id,
        empresa_id=empresa_id,
        acao="neo.associacao_manual",
        entidade="neo_decisao",
        entidade_id=decisao.id,
        dados_antes=antes,
        dados_depois={
            "resultado": decisao.resultado,
            "estrategia": decisao.estrategia,
            "motivo": decisao.motivo,
            "transacao_id": transacao.id,
            "transacao_status": transacao.status,
            "conta_id": conta.id,
        },
    )

    resp = NeoDecisaoResponse.model_validate(decisao)
    resp.transacao_descricao = transacao.historico
    resp.transacao_valor = transacao.valor
    resp.transacao_dc = transacao.dc
    resp.agencia_id = transacao.agencia_id
    return resp
