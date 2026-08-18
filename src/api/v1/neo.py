"""Router do NEO (motor de matching automático) — /api/v1/empresas/{empresa_id}/neo"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.core.errors import ConflictError
from src.db.models import NeoDecisao, PlanoConta, Transacao
from src.db.session import get_db
from src.domain.auditoria import registrar_auditoria
from src.domain.neo.consultas import listar_decisoes as _listar_decisoes
from src.domain.neo.engine import NeoEngine
from src.schemas.neo import (
    NeoAssociarManualRequest,
    NeoDecisaoListResponse,
    NeoDecisaoResponse,
    NeoProcessarRequest,
    NeoResultado,
)
from src.schemas.types import Competencia

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
    termo: str | None = Query(
        default=None, description="Busca no histórico do extrato ou na descrição da regra"
    ),
    estrategia: str | None = Query(
        default=None,
        description="exato | substring | prefixo | todas_palavras | manual | contraparte",
    ),
    dc: str | None = Query(
        default=None,
        description="D/débito ou C/crédito (aceita a letra ou a palavra por extenso)",
    ),
    agencia_id: UUID | None = Query(default=None),
    conta_id: UUID | None = Query(default=None, description="Conta contábil usada na regra"),
    mes: Competencia | None = Query(default=None, description="Competência AAAA-MM"),
    valor_min: Decimal | None = Query(
        default=None, ge=0, description="Valor mínimo da transação (R$)"
    ),
    valor_max: Decimal | None = Query(
        default=None, ge=0, description="Valor máximo da transação (R$)"
    ),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoDecisaoListResponse:
    """Lista o log de decisões do NEO para a empresa, com busca textual e filtros.

    Filtros com valor desconhecido (`resultado`, `estrategia`, `dc`) respondem
    422 em vez de devolver lista vazia — a tela precisa distinguir "não há
    resultado" de "o filtro foi mandado errado".
    """
    return await _listar_decisoes(
        db,
        empresa_id,
        termo=termo,
        resultado=resultado,
        estrategia=estrategia,
        dc=dc,
        agencia_id=agencia_id,
        conta_id=conta_id,
        mes=mes,
        valor_min=valor_min,
        valor_max=valor_max,
        page=page,
        page_size=page_size,
    )


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

    Transação já contabilizada responde 409 (e não 422): é conflito de estado,
    não payload inválido. Chegar aqui hoje significa que a tela está com uma
    listagem velha em mãos — todo caminho que contabiliza uma transação encerra
    a decisão 'sem_regra' dela junto (`NeoEngine._registrar_decisao` e esta
    própria rota), então a linha não fica mais para trás como ficava antes.
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
    if transacao.status == "processada":
        # ConflictError (e não HTTPException) para a resposta sair no mesmo
        # envelope `{"message": ...}` que o resto da API — é o que a tela lê.
        raise ConflictError(
            message=(
                "Esta transação já foi contabilizada — ela não está mais na "
                "lista 'Sem Regra'. Atualize a tela para ver a situação atual."
            )
        )
    if transacao.status != "pendente":
        raise HTTPException(
            status_code=422,
            detail=(
                f"A transação está com status '{transacao.status}' e não pode "
                "ser associada."
            ),
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
