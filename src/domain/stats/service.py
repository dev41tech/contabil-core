"""Serviço de Estatísticas para o Dashboard."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgenciaBancaria, Comprovante, NotaFiscal, RegistroContabil, Transacao
from src.schemas.stats import AgenciaStats, MesStats, ResumoStats, StatsResponse

logger = structlog.get_logger(__name__)


def _add_month(dt: datetime) -> datetime:
    """Adiciona exatamente 1 mês a um datetime."""
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _subtract_months(dt: datetime, n: int) -> datetime:
    """Subtrai n meses de um datetime."""
    for _ in range(n):
        # ir para o 1º do mês corrente e subtrair 1 dia para obter mês anterior
        first = dt.replace(day=1)
        prev_last = first.replace(
            year=first.year - 1 if first.month == 1 else first.year,
            month=12 if first.month == 1 else first.month - 1,
            day=1,
        )
        dt = prev_last
    return dt


class StatsService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    async def obter(self, meses: int = 12) -> StatsResponse:
        agora = datetime.now(UTC)
        inicio = _subtract_months(agora, meses - 1).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

        resumo = await self._resumo()
        mensal = await self._mensal(inicio, meses)
        por_agencia = await self._por_agencia()

        return StatsResponse(resumo=resumo, mensal=mensal, por_agencia=por_agencia)

    # ── resumo global ─────────────────────────────────────────────────────────

    async def _resumo(self) -> ResumoStats:
        total_t = await self._count(Transacao, Transacao.empresa_id == self._empresa_id)
        total_r = await self._count(
            RegistroContabil,
            RegistroContabil.empresa_id == self._empresa_id,
            RegistroContabil.deleted_at.is_(None),
        )
        total_n = await self._count(
            NotaFiscal,
            NotaFiscal.empresa_id == self._empresa_id,
            NotaFiscal.deleted_at.is_(None),
        )
        total_c = await self._count(
            Comprovante,
            Comprovante.empresa_id == self._empresa_id,
            Comprovante.deleted_at.is_(None),
        )

        # Conciliados = transações que têm pelo menos um RegistroContabil
        conc_subq = (
            select(RegistroContabil.transacao_id)
            .where(
                RegistroContabil.empresa_id == self._empresa_id,
                RegistroContabil.transacao_id.is_not(None),
            )
            .distinct()
            .subquery()
        )
        total_conc = (
            await self._db.execute(
                select(func.count())
                .select_from(Transacao)
                .where(
                    Transacao.empresa_id == self._empresa_id,
                    Transacao.id.in_(select(conc_subq)),
                )
            )
        ).scalar_one()

        nao_conc = total_t - total_conc
        pct = round((total_conc / total_t * 100) if total_t else 0, 1)

        return ResumoStats(
            total_transacoes=total_t,
            total_conciliados=total_conc,
            total_nao_conciliados=nao_conc,
            total_registros=total_r,
            total_notas=total_n,
            total_comprovantes=total_c,
            percentual_conciliacao=pct,
        )

    # ── série mensal ──────────────────────────────────────────────────────────

    async def _mensal(self, inicio: datetime, meses: int) -> list[MesStats]:
        # Meses no intervalo
        chaves: list[str] = []
        cur = inicio
        agora = datetime.now(UTC)
        while cur <= agora and len(chaves) < meses:
            chaves.append(cur.strftime("%Y-%m"))
            cur = _add_month(cur)

        t_rows = (
            await self._db.execute(
                select(
                    func.to_char(Transacao.data, "YYYY-MM").label("mes"),
                    func.count().label("total"),
                ).where(
                    Transacao.empresa_id == self._empresa_id,
                    Transacao.data >= inicio,
                ).group_by("mes")
            )
        ).all()

        r_rows = (
            await self._db.execute(
                select(
                    func.to_char(RegistroContabil.data_lancamento, "YYYY-MM").label("mes"),
                    func.count().label("total"),
                ).where(
                    RegistroContabil.empresa_id == self._empresa_id,
                    RegistroContabil.data_lancamento >= inicio,
                    RegistroContabil.deleted_at.is_(None),
                ).group_by("mes")
            )
        ).all()

        c_rows = (
            await self._db.execute(
                select(
                    func.to_char(Comprovante.data_pagamento, "YYYY-MM").label("mes"),
                    func.count().label("total"),
                ).where(
                    Comprovante.empresa_id == self._empresa_id,
                    Comprovante.data_pagamento >= inicio,
                    Comprovante.deleted_at.is_(None),
                    Comprovante.data_pagamento.is_not(None),
                ).group_by("mes")
            )
        ).all()

        n_rows = (
            await self._db.execute(
                select(
                    func.to_char(NotaFiscal.data_emissao, "YYYY-MM").label("mes"),
                    func.count().label("total"),
                ).where(
                    NotaFiscal.empresa_id == self._empresa_id,
                    NotaFiscal.data_emissao >= inicio,
                    NotaFiscal.deleted_at.is_(None),
                ).group_by("mes")
            )
        ).all()

        t_map = {r.mes: r.total for r in t_rows}
        r_map = {r.mes: r.total for r in r_rows}
        c_map = {r.mes: r.total for r in c_rows}
        n_map = {r.mes: r.total for r in n_rows}

        return [
            MesStats(
                mes=m,
                transacoes=t_map.get(m, 0),
                registros=r_map.get(m, 0),
                comprovantes=c_map.get(m, 0),
                notas=n_map.get(m, 0),
            )
            for m in chaves
        ]

    # ── por agência ───────────────────────────────────────────────────────────

    async def _por_agencia(self) -> list[AgenciaStats]:
        agencias = (
            await self._db.execute(
                select(AgenciaBancaria).where(
                    AgenciaBancaria.empresa_id == self._empresa_id,
                    AgenciaBancaria.ativa == True,
                )
            )
        ).scalars().all()

        result = []
        for ag in agencias:
            total_ag = await self._count(
                Transacao,
                Transacao.empresa_id == self._empresa_id,
                Transacao.agencia_id == ag.id,
            )
            conc_ag = (
                await self._db.execute(
                    select(func.count())
                    .select_from(Transacao)
                    .where(
                        Transacao.empresa_id == self._empresa_id,
                        Transacao.agencia_id == ag.id,
                        Transacao.id.in_(
                            select(RegistroContabil.transacao_id).where(
                                RegistroContabil.empresa_id == self._empresa_id,
                                RegistroContabil.transacao_id.is_not(None),
                            ).distinct()
                        ),
                    )
                )
            ).scalar_one()

            descricao = f"{ag.banco_sigla} – Ag. {ag.agencia} / {ag.numero}"
            result.append(
                AgenciaStats(
                    agencia_id=str(ag.id),
                    descricao=descricao,
                    conciliados=conc_ag,
                    nao_conciliados=total_ag - conc_ag,
                )
            )

        return result

    # ── helper ────────────────────────────────────────────────────────────────

    async def _count(self, model, *where) -> int:
        q = select(func.count()).select_from(model)
        if where:
            q = q.where(*where)
        return (await self._db.execute(q)).scalar_one()
