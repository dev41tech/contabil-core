"""Consultas de leitura sobre o NEO — separado de `engine.py` (processamento).

Existe porque `GET /neo/decisoes` só tinha `resultado`/`page`/`page_size` e o
router fazia SELECT + batch-fetch direto (ver item 4 do PDF de feedback dos
contadores: "seria possível colocar no NEO uma opção de busca/filtro?"). Os
filtros usam apenas dados que já existem em `NeoDecisao`/`Transacao`/`Regra` —
nada aqui depende de `Contraparte` porque a decisão persiste a conta aplicada,
mas não a proveniência completa da contraparte.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from src.core.dates import bounds_do_mes
from src.core.errors import ValidationError
from src.core.texto import normalizar_para_match, remover_acentos
from src.db.functions import sem_acento
from src.db.models import NeoDecisao, PlanoConta, Regra, Transacao
from src.schemas.neo import NeoDecisaoListResponse, NeoDecisaoResponse

RESULTADOS_VALIDOS = ("associada", "sem_regra", "erro")
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
