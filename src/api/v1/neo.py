"""Router do NEO (motor de matching automático) — /api/v1/empresas/{empresa_id}/neo"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_company_context, require_csrf
from src.core.errors import ConflictError, ValidationError
from src.db.models import NeoDecisao, PlanoConta, Transacao
from src.db.session import get_db
from src.domain.auditoria import registrar_auditoria
from src.domain.neo.consultas import (
    agrupar_pendencias as _agrupar_pendencias,
    consultar_divergencias as _consultar_divergencias,
    listar_decisoes as _listar_decisoes,
    simular_regra as _simular_regra,
)
from src.domain.neo.engine import NeoEngine
from src.domain.regras.service import RegraService
from src.schemas.neo import (
    NeoAssociarManualRequest,
    NeoClassificarLoteRequest,
    NeoClassificarLoteResponse,
    NeoCriarRegraEAplicarRequest,
    NeoCriarRegraEAplicarResponse,
    NeoDecisaoListResponse,
    NeoDecisaoResponse,
    NeoDivergenciasResponse,
    NeoPendenciasAgrupadasResponse,
    NeoProcessarRequest,
    NeoResultado,
    NeoSimularRegraRequest,
    NeoSimularRegraResponse,
)
from src.schemas.regras import RegraCreate
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
        description=(
            "exato | substring | todas_palavras | manual | contraparte "
            "(prefixo é aceito apenas como legado)"
        ),
    ),
    dc: str | None = Query(
        default=None,
        description="D/débito ou C/crédito (aceita a letra ou a palavra por extenso)",
    ),
    agencia_id: UUID | None = Query(default=None),
    conta_id: UUID | None = Query(default=None, description="Conta contábil da decisão"),
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


# A consulta foi criada nesta semana para a futura tela de conflitos; retirar a
# capacidade só porque essa tela ainda não chegou inverteria a dependência.
@router.get("/divergencias", response_model=NeoDivergenciasResponse)
async def consultar_divergencias(
    empresa_id: UUID,
    mes: Competencia | None = Query(default=None, description="Competência AAAA-MM"),
    agencia_id: UUID | None = Query(default=None),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoDivergenciasResponse:
    """Agrega conflitos medidos entre a conta da regra e a da contraparte."""
    return await _consultar_divergencias(
        db,
        empresa_id,
        mes=mes,
        agencia_id=agencia_id,
    )


@router.get(
    "/pendencias/agrupadas",
    response_model=NeoPendenciasAgrupadasResponse,
)
async def agrupar_pendencias(
    empresa_id: UUID,
    agencia_id: UUID | None = Query(default=None),
    mes: Competencia | None = Query(default=None, description="Competência AAAA-MM"),
    tokens: int = Query(default=3, ge=1, le=6),
    limite_grupos: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoPendenciasAgrupadasResponse:
    """Resume a fila pendente por padrão bancário sem perder os IDs acionáveis."""
    return await _agrupar_pendencias(
        db,
        empresa_id,
        agencia_id=agencia_id,
        mes=mes,
        tokens=tokens,
        limite_grupos=limite_grupos,
    )


@router.post(
    "/pendencias/classificar-lote",
    response_model=NeoClassificarLoteResponse,
    dependencies=[Depends(require_csrf)],
)
async def classificar_lote(
    empresa_id: UUID,
    body: NeoClassificarLoteRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoClassificarLoteResponse:
    """Classifica até 200 pendências sob os mesmos conta e histórico.

    O teto acompanha o maior `page_size` da API: cobre uma seleção completa da
    tela sem manter centenas ou milhares de locks até o commit. IDs ausentes,
    de outra empresa ou já processados são ignorados porque o lote representa
    uma fotografia que pode ter envelhecido entre a leitura e o clique.
    """
    conta = (
        await db.execute(
            select(PlanoConta).where(
                PlanoConta.id == body.conta_id,
                PlanoConta.empresa_id == empresa_id,
                PlanoConta.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conta is None:
        raise HTTPException(
            status_code=422, detail="Conta contábil não encontrada nesta empresa."
        )

    pendentes = (
        await db.execute(
            select(Transacao)
            .where(
                Transacao.id.in_(body.transacao_ids),
                Transacao.empresa_id == empresa_id,
                Transacao.status == "pendente",
            )
            .with_for_update()
        )
    ).scalars().all()
    por_id = {transacao.id: transacao for transacao in pendentes}
    ordenadas = [por_id[id_] for id_ in body.transacao_ids if id_ in por_id]
    ids_ignorados = [id_ for id_ in body.transacao_ids if id_ not in por_id]

    engine = NeoEngine(db=db, empresa_id=empresa_id)
    decisoes = await engine.classificar_manualmente_lote(
        ordenadas, conta.id, body.descricao
    )
    await db.flush()
    for transacao, decisao in zip(ordenadas, decisoes, strict=True):
        await registrar_auditoria(
            db,
            tenant_id=ctx.tenant_id,
            usuario_id=ctx.user_id,
            empresa_id=empresa_id,
            acao="neo.associacao_manual",
            entidade="neo_decisao",
            entidade_id=decisao.id,
            dados_antes={
                "resultado": "sem_regra",
                "transacao_status": "pendente",
            },
            dados_depois={
                "resultado": "associada",
                "estrategia": "manual",
                "motivo": decisao.motivo,
                "transacao_id": transacao.id,
                "transacao_status": transacao.status,
                "conta_id": conta.id,
            },
        )

    return NeoClassificarLoteResponse(
        classificadas=len(ordenadas),
        ignoradas=len(ids_ignorados),
        ids_ignorados=ids_ignorados,
    )


@router.post(
    "/pendencias/simular-regra",
    response_model=NeoSimularRegraResponse,
)
async def simular_regra(
    empresa_id: UUID,
    body: NeoSimularRegraRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoSimularRegraResponse:
    """Mostra o alcance e as contradições de uma regra sem persistir nada."""
    return await _simular_regra(
        db,
        empresa_id,
        historico=body.historico,
        dc=body.dc,
        agencia_id=body.agencia_id,
        conta_id=body.conta_id,
    )


@router.post(
    "/pendencias/criar-regra-e-aplicar",
    response_model=NeoCriarRegraEAplicarResponse,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def criar_regra_e_aplicar(
    empresa_id: UUID,
    body: NeoCriarRegraEAplicarRequest,
    ctx: AuthContext = Depends(get_company_context),
    db: AsyncSession = Depends(get_db),
) -> NeoCriarRegraEAplicarResponse:
    """Cria a regra validada e executa o NEO só no escopo revisado na tela."""
    # Regra `manual` não é aplicada pelo motor (`_carregar_regras` só carrega
    # `automatica`). Num endpoint cujo contrato é "cria e aplica", aceitá-la
    # seria devolver zero associações sem explicar por quê — a mesma armadilha
    # que a Fase 1 documentou no cadastro de regras.
    if body.tipo != "automatica":
        raise ValidationError(
            message=(
                "Só regra automática pode ser criada e aplicada de uma vez: "
                "regra manual não é usada pelo motor. Para classificar à mão, "
                "use a associação manual da pendência."
            )
        )
    dados_regra = RegraCreate.model_validate(body.model_dump(exclude={"mes"}))
    regra = await RegraService(db=db, empresa_id=empresa_id).criar(dados_regra)
    resultado = await NeoEngine(db=db, empresa_id=empresa_id).processar(
        agencia_id=body.agencia_id, mes=body.mes
    )
    return NeoCriarRegraEAplicarResponse(regra=regra, resultado=resultado)


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
    # O mesmo fluxo usado pelo lote encerra a decisão `sem_regra`; manter essa
    # transição no motor evita que as duas rotas voltem a divergir.
    decisao = (
        await engine.classificar_manualmente_lote(
            [transacao], conta.id, body.descricao
        )
    )[0]

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
    resp.conta_codigo = conta.codigo
    resp.conta_descricao = conta.descricao
    return resp
