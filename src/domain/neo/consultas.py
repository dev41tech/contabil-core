"""Consultas de leitura sobre o NEO — separado de `engine.py` (processamento).

Existe porque `GET /neo/decisoes` só tinha `resultado`/`page`/`page_size` e o
router fazia SELECT + batch-fetch direto (ver item 4 do PDF de feedback dos
contadores: "seria possível colocar no NEO uma opção de busca/filtro?"). Os
filtros usam apenas dados persistidos em `NeoDecisao`/`Transacao`/`Regra`.
Desde a migration 0023, a decisão também guarda a medição de contraparte do
shadow mode; isso permite o relatório agregado sem reler documentos fiscais.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, contains_eager

from src.core.dates import bounds_do_mes_data
from src.core.errors import ValidationError
from src.core.texto import (
    chave_agrupamento_historico,
    normalizar_para_match,
    remover_acentos,
)
from src.db.functions import sem_acento
from src.db.models import (
    AgenciaBancaria,
    ExtratoImportacao,
    NeoDecisao,
    PlanoConta,
    Regra,
    RegistroContabil,
    Transacao,
    Usuario,
)
from src.domain.neo.engine import ESTRATEGIA_VALOR_SUSPEITO, estrategia_de_match
from src.schemas.neo import (
    NeoConflitoAmostra,
    NeoDecisaoListResponse,
    NeoDecisaoResponse,
    NeoDivergenciaAmostraResponse,
    NeoDivergenciaPorContaResponse,
    NeoDivergenciasResponse,
    NeoPendenciaGrupoResponse,
    NeoPendenciasAgrupadasResponse,
    NeoSimulacaoConflitos,
    NeoSimulacaoQuantidade,
    NeoSimulacaoResumo,
    NeoSimularRegraResponse,
)

RESULTADOS_VALIDOS = ("associada", "sem_regra", "erro")
TETO_PENDENCIAS_AGRUPAMENTO = 10_000
LIMITE_AMOSTRA_DIVERGENCIAS = 10
ESTRATEGIAS_VALIDAS = (
    "exato",
    "substring",
    # Legado inalcançável: continua aceito para links e filtros salvos não
    # passarem a responder 422 depois da remoção da estratégia do motor.
    "prefixo",
    "todas_palavras",
    "manual",
    "contraparte",
    # Não é uma forma de casar histórico: marca a pendência que o motor
    # recusou contabilizar por valor não confiável. Fica aqui porque a fila
    # filtra por este mesmo campo e a tela precisa isolar essas linhas. Vem do
    # motor, e não repetida como texto, para o filtro não passar a mentir se a
    # marca mudar de nome.
    ESTRATEGIA_VALOR_SUSPEITO,
)

# Aceita a letra e a palavra por extenso, com ou sem acento.
#
# Correção de rota: este bloco nasceu com a hipótese de que o front mandava
# "débito"/"crédito" por extenso e que era isso o "filtro de crédito/débito não
# funciona" relatado pelo escritório. Era falso — o front sempre mandou "D"/"C",
# e havia teste cobrindo. A queixa real era outra: a tabela do NEO não exibia
# valor nem D/C, então filtrar não mudava nada visível na tela e parecia
# quebrado. Resolvido no front, com as colunas.
#
# A tolerância aqui fica porque é barata e vale por si: antes, qualquer valor
# diferente de "D"/"C" virava um WHERE que não casava com nada e a tela
# devolvia lista vazia sem dizer por quê. Hoje valor desconhecido responde 422.
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
    data_de: date | None = None,
    data_ate: date | None = None,
    motivo: str | None = None,
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
    # `mes` e o intervalo se ACUMULAM, não competem. A competência é global na
    # aplicação — o contador a escolhe uma vez e ela vale em todas as telas —,
    # então um intervalo informado aqui é um recorte DENTRO dela, mais
    # específico. Dar precedência ao mês descartaria justamente a escolha mais
    # explícita; pedir mês e intervalo incompatíveis devolve vazio, que é a
    # resposta honesta.
    if mes:
        inicio, fim = bounds_do_mes_data(mes)
        q = q.where(Transacao.data >= inicio, Transacao.data <= fim)
    if data_de is not None and data_ate is not None and data_de > data_ate:
        raise ValidationError(message="data_de não pode ser maior que data_ate.")
    if data_de is not None:
        q = q.where(Transacao.data >= data_de)
    if data_ate is not None:
        q = q.where(Transacao.data <= data_ate)
    if motivo:
        # O motivo é escrito pelo motor, sem acento inconsistente, mas passa
        # pela mesma normalização do `termo` para o contador não precisar saber
        # disso.
        motivo_like = f"%{_escapar_ilike(remover_acentos(motivo.strip()).lower())}%"
        q = q.where(sem_acento(NeoDecisao.motivo).like(motivo_like, escape="\\"))
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

    # Resolve o lançamento vigente em lote — a alternativa é uma query por linha.
    # Só as decisões que viraram partida têm lançamento; as pendentes ficam com
    # None, e a tela usa isso para saber quando oferecer "Desfazer".
    lancamento_por_transacao: dict[UUID, UUID] = {}
    transacao_ids = [r.transacao_id for r in rows]
    if transacao_ids:
        vinculos = (
            await db.execute(
                select(RegistroContabil.transacao_id, RegistroContabil.lancamento_id)
                .where(
                    RegistroContabil.transacao_id.in_(transacao_ids),
                    RegistroContabil.deleted_at.is_(None),
                )
            )
        ).all()
        lancamento_por_transacao = {t_id: l_id for t_id, l_id in vinculos}

    items = []
    for r in rows:
        item = NeoDecisaoResponse.model_validate(r)
        item.lancamento_id = lancamento_por_transacao.get(r.transacao_id)
        item.transacao_descricao = r.transacao.historico
        item.transacao_valor = r.transacao.valor
        item.transacao_dc = r.transacao.dc
        item.transacao_data = r.transacao.data
        item.agencia_id = r.transacao.agencia_id
        item.regra_descricao = r.regra.descricao if r.regra else None
        item.conta_codigo = r.conta.codigo if r.conta else None
        item.conta_descricao = r.conta.descricao if r.conta else None
        items.append(item)

    return NeoDecisaoListResponse(items=items, total=total, page=page, page_size=page_size)


async def consultar_divergencias(
    db: AsyncSession,
    empresa_id: UUID,
    *,
    mes: str | None = None,
    agencia_id: UUID | None = None,
) -> NeoDivergenciasResponse:
    """Resume as medições reais do shadow mode e seus conflitos financeiros.

    `conta_divergente IS NOT NULL` é o marcador de que houve medição. Em
    especial, FALSE entra em `total_avaliadas`: filtrá-lo como truthy faria o
    denominador conter só conflitos e produzir um percentual enganoso.
    """
    filtros = [
        NeoDecisao.empresa_id == empresa_id,
        NeoDecisao.conta_divergente.is_not(None),
    ]
    if agencia_id is not None:
        filtros.append(Transacao.agencia_id == agencia_id)
    if mes is not None:
        inicio, fim = bounds_do_mes_data(mes)
        filtros.extend((Transacao.data >= inicio, Transacao.data <= fim))

    resumo = (
        await db.execute(
            select(
                func.count(NeoDecisao.id),
                func.sum(
                    case((NeoDecisao.conta_divergente.is_(True), 1), else_=0)
                ),
                func.sum(
                    case(
                        (NeoDecisao.conta_divergente.is_(True), Transacao.valor),
                        else_=Decimal("0"),
                    )
                ),
            )
            .join(Transacao, Transacao.id == NeoDecisao.transacao_id)
            .where(*filtros)
        )
    ).one()
    total_avaliadas = int(resumo[0] or 0)
    total_divergentes = int(resumo[1] or 0)
    valor_total_divergente = Decimal(str(resumo[2] or Decimal("0.00")))

    conta_regra = aliased(PlanoConta)
    conta_contraparte = aliased(PlanoConta)
    grupos = (
        await db.execute(
            select(
                NeoDecisao.conta_id,
                conta_regra.codigo,
                conta_regra.descricao,
                NeoDecisao.conta_contraparte_id,
                conta_contraparte.codigo,
                conta_contraparte.descricao,
                func.count(NeoDecisao.id),
                func.sum(Transacao.valor),
            )
            .join(Transacao, Transacao.id == NeoDecisao.transacao_id)
            .join(conta_regra, conta_regra.id == NeoDecisao.conta_id)
            .join(
                conta_contraparte,
                conta_contraparte.id == NeoDecisao.conta_contraparte_id,
            )
            .where(*filtros, NeoDecisao.conta_divergente.is_(True))
            .group_by(
                NeoDecisao.conta_id,
                conta_regra.codigo,
                conta_regra.descricao,
                NeoDecisao.conta_contraparte_id,
                conta_contraparte.codigo,
                conta_contraparte.descricao,
            )
            .order_by(func.sum(Transacao.valor).desc())
        )
    ).all()
    por_conta = [
        NeoDivergenciaPorContaResponse(
            conta_regra_id=row[0],
            conta_regra_codigo=row[1],
            conta_regra_descricao=row[2],
            conta_contraparte_id=row[3],
            conta_contraparte_codigo=row[4],
            conta_contraparte_descricao=row[5],
            quantidade=row[6],
            valor_total=row[7],
        )
        for row in grupos
    ]

    casos = (
        await db.execute(
            select(
                NeoDecisao.id,
                Transacao.id,
                Transacao.historico,
                Transacao.valor,
                NeoDecisao.origem_evidencia,
                NeoDecisao.contraparte_id,
                NeoDecisao.conta_id,
                conta_regra.codigo,
                conta_regra.descricao,
                NeoDecisao.conta_contraparte_id,
                conta_contraparte.codigo,
                conta_contraparte.descricao,
            )
            .join(Transacao, Transacao.id == NeoDecisao.transacao_id)
            .join(conta_regra, conta_regra.id == NeoDecisao.conta_id)
            .join(
                conta_contraparte,
                conta_contraparte.id == NeoDecisao.conta_contraparte_id,
            )
            .where(*filtros, NeoDecisao.conta_divergente.is_(True))
            # Os maiores valores são os casos mais úteis para uma decisão de
            # produto; IDs estabilizam o desempate entre valores iguais.
            .order_by(Transacao.valor.desc(), NeoDecisao.id.asc())
            .limit(LIMITE_AMOSTRA_DIVERGENCIAS)
        )
    ).all()
    amostra = [
        NeoDivergenciaAmostraResponse(
            decisao_id=row[0],
            transacao_id=row[1],
            historico=row[2],
            valor=row[3],
            origem_evidencia=row[4],
            contraparte_id=row[5],
            conta_regra_id=row[6],
            conta_regra_codigo=row[7],
            conta_regra_descricao=row[8],
            conta_contraparte_id=row[9],
            conta_contraparte_codigo=row[10],
            conta_contraparte_descricao=row[11],
        )
        for row in casos
    ]

    percentual = (
        round(total_divergentes * 100 / total_avaliadas, 2)
        if total_avaliadas
        else 0.0
    )
    return NeoDivergenciasResponse(
        total_avaliadas=total_avaliadas,
        total_divergentes=total_divergentes,
        percentual_divergentes=percentual,
        valor_total_divergente=valor_total_divergente,
        por_conta=por_conta,
        amostra=amostra,
    )


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
        Transacao.deleted_at.is_(None),
    ]
    if agencia_id:
        filtros.append(Transacao.agencia_id == agencia_id)
    if mes:
        inicio, fim = bounds_do_mes_data(mes)
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


async def listar_desfeitas(
    db: AsyncSession,
    empresa_id: UUID,
    *,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Lançamentos cancelados, do mais recente para o mais antigo.

    Uma linha por LANÇAMENTO, não por partida: o par é a unidade, e listar as
    duas linhas mostraria o mesmo cancelamento duas vezes. A partida escolhida
    para exibição é a da conta classificada, não a contrapartida bancária — é a
    conta que o contador reconhece.

    Quando o cancelamento veio de um lote, `importacao_id` e `importacao_arquivo`
    vêm preenchidos, e é isso que permite a tela agrupar os itens sob o arquivo
    que os trouxe em vez de espalhá-los numa lista plana.
    """
    conta_bancaria = aliased(PlanoConta)

    # Das duas partidas do par, a exibida é a da CLASSIFICAÇÃO, não a
    # contrapartida bancária. O discriminador é estrutural, não textual: o par
    # sempre tem D/C opostos, e a partida de classificação carrega o mesmo D/C
    # da transação — regra só é candidata quando `regra.dc == transacao.dc`
    # (engine.py:310), e os caminhos manual e por contraparte passam
    # `transacao.dc` diretamente.
    #
    # Filtrar pela descrição ("Contrapartida bancária: ...") pareceria mais
    # óbvio e estaria errado: `normalizar_historico_contabil` põe tudo em
    # maiúsculas antes de gravar, então o prefixo no banco não é o do código.
    #
    # A comparação de D/C aqui já foi um `cast` para texto dos dois lados, e
    # não por gosto: `RegistroContabil.dc` e `Transacao.dc` eram enums
    # DIFERENTES no PostgreSQL, e comparar dois enums distintos é erro de tipo
    # ("operator does not exist: dc_registro_enum = dc_transacao_enum"). Em
    # SQLite enum é texto e a comparação passava — foi assim que a suíte deu
    # verde com este endpoint quebrado em produção. A migration 0031 unificou
    # os três tipos em `dc_enum`, então a comparação volta a ser direta. O
    # remendo saiu junto com a causa; se voltar a aparecer `cast` em cima de
    # `dc`, é sinal de que alguém criou um enum novo por tabela de novo.
    base = (
        select(RegistroContabil, Transacao, ExtratoImportacao, Usuario)
        .join(Transacao, Transacao.id == RegistroContabil.transacao_id)
        .outerjoin(
            ExtratoImportacao, ExtratoImportacao.id == Transacao.importacao_id
        )
        .outerjoin(Usuario, Usuario.id == RegistroContabil.cancelado_por)
        .where(
            RegistroContabil.empresa_id == empresa_id,
            RegistroContabil.cancelado_em.is_not(None),
            RegistroContabil.dc == Transacao.dc,
        )
    )

    total = (
        await db.execute(
            select(func.count()).select_from(
                base.with_only_columns(RegistroContabil.id).subquery()
            )
        )
    ).scalar_one()

    linhas = (
        await db.execute(
            base.order_by(RegistroContabil.cancelado_em.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    # A tela oferece "Associar" direto daqui, e para isso precisa da decisão
    # PENDENTE da transação — `associar_manual` recebe `decisao_id`, não
    # `transacao_id`. Buscada em lote: uma query para a página, não uma por
    # linha.
    #
    # A decisão certa é a MAIS RECENTE `sem_regra`: o cancelamento cria uma
    # nova linha de log, então a antiga (`associada`) continua lá e associar
    # contra ela seria associar contra a classificação que acabou de ser
    # desfeita.
    decisao_pendente: dict[UUID, UUID] = {}
    transacao_ids = [linha[1].id for linha in linhas]
    if transacao_ids:
        recentes = (
            await db.execute(
                select(NeoDecisao.transacao_id, NeoDecisao.id)
                .where(
                    NeoDecisao.transacao_id.in_(transacao_ids),
                    NeoDecisao.resultado == "sem_regra",
                )
                .order_by(NeoDecisao.processado_em.asc())
            )
        ).all()
        # A ordem crescente faz a última escrita vencer — sobra a mais recente.
        decisao_pendente = {t_id: d_id for t_id, d_id in recentes}

    itens: list[dict] = []
    for registro, transacao, importacao, usuario in linhas:
        itens.append(
            {
                "lancamento_id": registro.lancamento_id,
                "transacao_id": transacao.id,
                "transacao_data": transacao.data,
                "transacao_descricao": transacao.historico,
                "valor": registro.valor,
                "dc": registro.dc,
                "conta_descricao": registro.descricao,
                "cancelado_em": registro.cancelado_em,
                "cancelado_por_nome": usuario.nome if usuario else None,
                "motivo_cancelamento": registro.motivo_cancelamento,
                "importacao_id": importacao.id if importacao else None,
                "importacao_arquivo": importacao.nome_arquivo if importacao else None,
                # Distingue "veio de um lote que foi cancelado inteiro" de "veio
                # de um lote, mas foi desfeito sozinho" — a tela agrupa só o
                # primeiro caso.
                "lote_cancelado": bool(importacao and importacao.cancelada_em),
                # A tela só oferece "Associar" enquanto a transação estiver de
                # volta na fila. Se ela já foi reclassificada, o botão levaria a
                # um 409 e o certo é mostrar que o caso já foi resolvido.
                "transacao_status": transacao.status,
                # Enquanto marcada, o motor não toca nesta transação. A tela usa
                # isto para explicar por que ela não volta sozinha e oferecer a
                # liberação a quem desfez por engano.
                "aguardando_decisao_manual": transacao.auto_recusado_em is not None,
                "decisao_atual_id": (
                    decisao_pendente.get(transacao.id)
                    if transacao.status == "pendente"
                    else None
                ),
            }
        )
    return itens, total
