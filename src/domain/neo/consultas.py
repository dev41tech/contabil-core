"""Consultas de leitura sobre o NEO — separado de `engine.py` (processamento).

Existe porque `GET /neo/decisoes` só tinha `resultado`/`page`/`page_size` e o
router fazia SELECT + batch-fetch direto (ver item 4 do PDF de feedback dos
contadores: "seria possível colocar no NEO uma opção de busca/filtro?"). Os
filtros usam apenas dados que já existem em `Transacao`/`Regra` — nada aqui
depende de `Contraparte` porque o NEO ainda não persiste proveniência de
contraparte (isso é shadow mode, só em log, ver `engine.py`).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from src.core.dates import bounds_do_mes
from src.db.models import NeoDecisao, Regra, Transacao
from src.schemas.neo import NeoDecisaoListResponse, NeoDecisaoResponse


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
        .where(NeoDecisao.empresa_id == empresa_id)
    )

    if resultado:
        q = q.where(NeoDecisao.resultado == resultado)
    if estrategia:
        q = q.where(NeoDecisao.estrategia == estrategia)
    if dc:
        q = q.where(Transacao.dc == dc.strip().upper())
    if agencia_id:
        q = q.where(Transacao.agencia_id == agencia_id)
    if conta_id:
        q = q.where(Regra.conta_id == conta_id)
    if mes:
        inicio, fim = bounds_do_mes(mes)
        q = q.where(Transacao.data >= inicio, Transacao.data <= fim)
    if termo:
        termo_like = f"%{_escapar_ilike(termo.strip())}%"
        q = q.where(
            or_(
                Transacao.historico.ilike(termo_like, escape="\\"),
                Regra.descricao.ilike(termo_like, escape="\\"),
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
                    contains_eager(NeoDecisao.transacao), contains_eager(NeoDecisao.regra)
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
        items.append(item)

    return NeoDecisaoListResponse(items=items, total=total, page=page, page_size=page_size)


def _escapar_ilike(termo: str) -> str:
    """Escapa `%`/`_`/barra para que o termo digitado não vire wildcard SQL."""
    return termo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
