"""Consultas de leitura sobre o NEO — separado de `engine.py` (processamento).

Existe porque `GET /neo/decisoes` só tinha `resultado`/`page`/`page_size` e o
router fazia SELECT + batch-fetch direto (ver item 4 do PDF de feedback dos
contadores: "seria possível colocar no NEO uma opção de busca/filtro?"). Os
filtros usam apenas dados que já existem em `NeoDecisao`/`Transacao`/`Regra` —
nada aqui depende de `Contraparte` porque a decisão persiste a conta aplicada,
mas não a proveniência completa da contraparte.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from src.core.dates import bounds_do_mes
from src.core.errors import ValidationError
from src.core.texto import (
    chave_agrupamento_historico,
    normalizar_para_match,
    remover_acentos,
)
from src.db.functions import sem_acento
from src.db.models import (
    AgenciaBancaria,
    NeoDecisao,
    PlanoConta,
    Regra,
    RegistroContabil,
    Transacao,
)
from src.domain.neo.engine import estrategia_de_match
from src.schemas.neo import (
    NeoConflitoAmostra,
    NeoDecisaoListResponse,
    NeoDecisaoResponse,
    NeoPendenciaGrupoResponse,
    NeoPendenciasAgrupadasResponse,
    NeoSimulacaoConflitos,
    NeoSimulacaoQuantidade,
    NeoSimulacaoResumo,
    NeoSimularRegraResponse,
)

RESULTADOS_VALIDOS = ("associada", "sem_regra", "erro")
TETO_PENDENCIAS_AGRUPAMENTO = 10_000
ESTRATEGIAS_VALIDAS = (
    "exato",
    "substring",
    # Legado inalcançável: continua aceito para links e filtros salvos não
    # passarem a responder 422 depois da remoção da estratégia do motor.
    "prefixo",
    "todas_palavras",
    "manual",
    "contraparte",
)

# O front manda ora "D"/"C", ora a palavra por extenso (com ou sem acento).
# Antes, qualquer coisa diferente de "D"/"C" virava um WHERE que não casava com
# nada e a tela mostrava lista vazia sem dizer por quê — era o "filtro de
# crédito/débito não funciona" relatado pelo escritório.
_DC_ACEITOS = {
    "d": "D",
    "debito": "D",
    "debitos": "D",
    "c": "C",
    "credito": "C",
    "creditos": "C",
}


def normalizar_dc(dc: str) -> str:
    """'débito', 'Debito', 'd', 'D' → 'D'. Valor desconhecido vira 422."""
    chave = normalizar_para_match(dc)
    valor = _DC_ACEITOS.get(chave)
    if valor is None:
        raise ValidationError(
            message="dc deve ser 'D' (débito) ou 'C' (crédito)."
        )
    return valor


def _validar_opcao(valor: str, aceitos: tuple[str, ...], campo: str) -> str:
    normalizado = valor.strip().lower()
    if normalizado not in aceitos:
        raise ValidationError(
            message=f"{campo} deve ser um de: {', '.join(aceitos)}."
        )
    return normalizado


async def listar_decisoes(
    db: AsyncSession,
    empresa_id: UUID,
    *,
    termo: str | None = None,
    resultado: str | None = None,
    estrategia: str | None = None,
    dc: str | None = None,
    agencia_id: UUID | None = None,
    conta_id: UUID | None = None,
    mes: str | None = None,
    valor_min: Decimal | None = None,
    valor_max: Decimal | None = None,
    page: int = 1,
    page_size: int = 50,
) -> NeoDecisaoListResponse:
    """Lista o log de decisões do NEO para a empresa, com busca textual e filtros.

    `termo` busca em `Transacao.historico` (o texto do extrato) e
    `Regra.descricao` (a classificação aplicada) — os dois campos que um
    contador reconheceria ao procurar um lançamento específico.
    """
    q = (
        select(NeoDecisao)
        .join(Transacao, Transacao.id == NeoDecisao.transacao_id)
        .outerjoin(Regra, Regra.id == NeoDecisao.regra_id)
        .outerjoin(PlanoConta, PlanoConta.id == NeoDecisao.conta_id)
        .where(NeoDecisao.empresa_id == empresa_id)
    )

    if resultado:
        q = q.where(
            NeoDecisao.resultado
            == _validar_opcao(resultado, RESULTADOS_VALIDOS, "resultado")
        )
    if estrategia:
        q = q.where(
            NeoDecisao.estrategia
            == _validar_opcao(estrategia, ESTRATEGIAS_VALIDAS, "estrategia")
        )
    if dc:
        q = q.where(Transacao.dc == normalizar_dc(dc))
    if agencia_id:
        q = q.where(Transacao.agencia_id == agencia_id)
    if conta_id:
        q = q.where(
            or_(
                NeoDecisao.conta_id == conta_id,
                and_(NeoDecisao.conta_id.is_(None), Regra.conta_id == conta_id),
            )
        )
    if mes:
        inicio, fim = bounds_do_mes(mes)
        q = q.where(Transacao.data >= inicio, Transacao.data <= fim)
    if valor_min is not None and valor_max is not None and valor_min > valor_max:
        raise ValidationError(message="valor_min não pode ser maior que valor_max.")
    # `Transacao.valor` é gravado em módulo pelo importador de extrato (o sinal
    # do lançamento está em `dc`), então a faixa é sempre em números positivos —
    # o contador digita "150", não "-150", para achar um débito de R$ 150,00.
    if valor_min is not None:
        q = q.where(Transacao.valor >= valor_min)
    if valor_max is not None:
        q = q.where(Transacao.valor <= valor_max)
    if termo:
        # Busca com acento dobrado dos dois lados: quem digita "liquidacao"
        # precisa achar "LIQUIDAÇÃO" no extrato, e vice-versa. Pontuação é
        # preservada de propósito — buscar por "12.345" ou "PIX-ENVIADO" tem
        # que continuar funcionando.
        termo_like = f"%{_escapar_ilike(remover_acentos(termo.strip()).lower())}%"
        q = q.where(
            or_(
                sem_acento(Transacao.historico).like(termo_like, escape="\\"),
                sem_acento(Regra.descricao).like(termo_like, escape="\\"),
            )
        )

    count_q = select(func.count()).select_from(
        q.with_only_columns(NeoDecisao.id).subquery()
    )
    total = (await db.execute(count_q)).scalar_one()

    rows = (
        (
            await db.execute(
                q.options(
                    contains_eager(NeoDecisao.transacao),
                    contains_eager(NeoDecisao.regra),
                    contains_eager(NeoDecisao.conta),
                )
                .order_by(NeoDecisao.processado_em.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .unique()
        .scalars()
        .all()
    )

    items = []
    for r in rows:
        item = NeoDecisaoResponse.model_validate(r)
        item.transacao_descricao = r.transacao.historico
        item.transacao_valor = r.transacao.valor
        item.transacao_dc = r.transacao.dc
        item.agencia_id = r.transacao.agencia_id
        item.regra_descricao = r.regra.descricao if r.regra else None
        item.conta_codigo = r.conta.codigo if r.conta else None
        item.conta_descricao = r.conta.descricao if r.conta else None
        items.append(item)

    return NeoDecisaoListResponse(items=items, total=total, page=page, page_size=page_size)


def _escapar_ilike(termo: str) -> str:
    """Escapa `%`/`_`/barra para que o termo digitado não vire wildcard SQL."""
    return termo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class _GrupoPendente:
    """Acumula um grupo sem transformar o histórico bruto usado na exibição."""

    padrao: str
    dc: str
    quantidade: int = 0
    valor_total: Decimal = Decimal("0")
    data_inicio: datetime | None = None
    data_fim: datetime | None = None
    agencia_ids: set[UUID] = field(default_factory=set)
    historicos: Counter[str] = field(default_factory=Counter)
    transacao_ids: list[UUID] = field(default_factory=list)

    def adicionar(self, transacao: Transacao) -> None:
        self.quantidade += 1
        self.valor_total += transacao.valor
        self.data_inicio = (
            transacao.data
            if self.data_inicio is None
            else min(self.data_inicio, transacao.data)
        )
        self.data_fim = (
            transacao.data
            if self.data_fim is None
            else max(self.data_fim, transacao.data)
        )
        self.agencia_ids.add(transacao.agencia_id)
        self.historicos[transacao.historico] += 1
        self.transacao_ids.append(transacao.id)

    def resposta(self) -> NeoPendenciaGrupoResponse:
        # Frequência aproxima o texto que o contador mais viu. O desempate
        # lexical torna rótulo e amostras estáveis mesmo que o banco mude a
        # ordem física das linhas entre duas chamadas.
        historicos_ordenados = sorted(
            self.historicos, key=lambda texto: (-self.historicos[texto], texto)
        )
        assert self.data_inicio is not None and self.data_fim is not None
        return NeoPendenciaGrupoResponse(
            padrao=self.padrao,
            rotulo=historicos_ordenados[0],
            dc=self.dc,
            quantidade=self.quantidade,
            valor_total=self.valor_total,
            data_inicio=self.data_inicio,
            data_fim=self.data_fim,
            agencia_ids=sorted(self.agencia_ids, key=str),
            amostras=historicos_ordenados[:5],
            transacao_ids=self.transacao_ids,
        )


async def agrupar_pendencias(
    db: AsyncSession,
    empresa_id: UUID,
    *,
    agencia_id: UUID | None = None,
    mes: str | None = None,
    tokens: int = 3,
    limite_grupos: int = 50,
) -> NeoPendenciasAgrupadasResponse:
    """Agrupa a fila real de pendências do NEO por padrão textual e lado D/C.

    A consulta carrega no máximo 10.000 transações. Acima disso, exige que a
    tela restrinja agência ou competência: truncar silenciosamente faria
    `total_grupos` mentir, enquanto carregar uma fila sem limite tornaria uma
    rota de leitura capaz de consumir memória sem controle.

    Agência não participa da chave deliberadamente. Uma regra pertence a uma
    agência, mas fragmentar aqui esconderia que o mesmo padrão ocorre em várias
    contas; `agencia_ids` permite à tela avisar quantas regras serão necessárias.
    `dc`, ao contrário, participa porque uma única regra nunca cobre os dois lados.
    """
    filtros = [
        NeoDecisao.empresa_id == empresa_id,
        NeoDecisao.resultado == "sem_regra",
        Transacao.status == "pendente",
    ]
    if agencia_id:
        filtros.append(Transacao.agencia_id == agencia_id)
    if mes:
        inicio, fim = bounds_do_mes(mes)
        filtros.extend((Transacao.data >= inicio, Transacao.data <= fim))

    base = (
        select(Transacao)
        .join(NeoDecisao, NeoDecisao.transacao_id == Transacao.id)
        .where(*filtros)
    )
    total_pendentes = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    # Acima do teto a varredura é truncada, não recusada. Recusar deixaria a
    # tela sem nada justamente no pior momento — logo após importar um ano de
    # extrato, quando ainda não existe regra nenhuma e tudo está pendente. O
    # `total_pendentes` vem de um COUNT sobre o conjunto inteiro, então os
    # números continuam honestos: `parcial` avisa que os grupos descrevem só
    # uma fatia.
    parcial = total_pendentes > TETO_PENDENCIAS_AGRUPAMENTO

    transacoes = (
        await db.execute(
            base.order_by(Transacao.data.asc(), Transacao.id.asc()).limit(
                TETO_PENDENCIAS_AGRUPAMENTO
            )
        )
    ).scalars().all()

    acumulados: dict[tuple[str, str], _GrupoPendente] = {}
    for transacao in transacoes:
        padrao = chave_agrupamento_historico(transacao.historico, tokens)
        chave = (padrao, transacao.dc)
        grupo = acumulados.setdefault(chave, _GrupoPendente(padrao, transacao.dc))
        grupo.adicionar(transacao)

    grupos = [grupo.resposta() for grupo in acumulados.values()]
    grupos.sort(
        key=lambda grupo: (
            -grupo.quantidade,
            -grupo.valor_total,
            grupo.padrao,
            grupo.dc,
        )
    )
    devolvidos = grupos[:limite_grupos]
    return NeoPendenciasAgrupadasResponse(
        grupos=devolvidos,
        total_pendentes=total_pendentes,
        total_agrupadas=sum(grupo.quantidade for grupo in devolvidos),
        total_grupos=len(grupos),
        parcial=parcial,
    )


async def simular_regra(
    db: AsyncSession,
    empresa_id: UUID,
    *,
    historico: str,
    dc: str,
    agencia_id: UUID,
    conta_id: UUID,
) -> NeoSimularRegraResponse:
    """Mede o impacto de uma regra sem persistir qualquer alteração.

    A varredura usa `estrategia_de_match`, a mesma função pura chamada por
    `NeoEngine._encontrar_regra`. Fazer o filtro textual no banco seria mais
    barato, mas criaria diferenças entre SQLite/Postgres e, principalmente,
    uma segunda implementação da normalização que poderia mentir na prévia.
    """
    agencia_existe = (
        await db.execute(
            select(AgenciaBancaria.id).where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == empresa_id,
                AgenciaBancaria.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    conta_existe = (
        await db.execute(
            select(PlanoConta.id).where(
                PlanoConta.id == conta_id,
                PlanoConta.empresa_id == empresa_id,
                PlanoConta.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if agencia_existe is None:
        raise ValidationError(message="Agência bancária não encontrada nesta empresa.")
    if conta_existe is None:
        raise ValidationError(message="Conta contábil não encontrada nesta empresa.")

    # O lançamento com o mesmo D/C da transação é a classificação; o outro é
    # a contrapartida bancária e não pode ser tratado como conflito.
    lancamento_classificacao = and_(
        RegistroContabil.transacao_id == Transacao.id,
        # Comparar duas colunas Enum distintas funciona no SQLite, mas o
        # Postgres não possui operador entre `dc_registro_enum` e
        # `dc_transacao_enum`. O literal já validado preserva a semântica nos
        # dois bancos sem depender de cast específico de fornecedor.
        RegistroContabil.dc == dc,
        RegistroContabil.deleted_at.is_(None),
    )
    consulta = (
        select(Transacao, RegistroContabil.conta_id)
        .outerjoin(RegistroContabil, lancamento_classificacao)
        .where(
            Transacao.empresa_id == empresa_id,
            Transacao.agencia_id == agencia_id,
            Transacao.dc == dc,
            Transacao.status.in_(("pendente", "processada")),
        )
        .order_by(Transacao.data.asc(), Transacao.id.asc())
        .execution_options(yield_per=500)
    )

    pendencias = contabilizadas = conflitos = 0
    amostras_pendencias: list[str] = []
    amostras_conflitos: list[NeoConflitoAmostra] = []
    resultado = await db.stream(consulta)
    async for transacao, conta_lancada_id in resultado:
        if estrategia_de_match(historico, transacao.historico) is None:
            continue
        if transacao.status == "pendente":
            pendencias += 1
            if transacao.historico not in amostras_pendencias and len(amostras_pendencias) < 5:
                amostras_pendencias.append(transacao.historico)
            continue
        # Status processada sem partidas não é uma transação contabilizada e
        # não deve inflar a prévia diante de um resíduo inconsistente.
        if conta_lancada_id is None:
            continue
        contabilizadas += 1
        if conta_lancada_id != conta_id:
            conflitos += 1
            if len(amostras_conflitos) < 5:
                amostras_conflitos.append(
                    NeoConflitoAmostra(
                        transacao_id=transacao.id,
                        historico=transacao.historico,
                        conta_id=conta_lancada_id,
                    )
                )

    return NeoSimularRegraResponse(
        pendencias_atingidas=NeoSimulacaoResumo(
            quantidade=pendencias, amostras=amostras_pendencias
        ),
        ja_contabilizadas_atingidas=NeoSimulacaoQuantidade(
            quantidade=contabilizadas
        ),
        conflitos=NeoSimulacaoConflitos(
            quantidade=conflitos, amostras=amostras_conflitos
        ),
    )
