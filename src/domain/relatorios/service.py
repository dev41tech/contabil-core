"""Serviço de relatórios financeiros: DRE, Balancete e Livro Caixa."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AgenciaBancaria, PlanoConta, RegistroContabil, Transacao
from src.schemas.relatorios import (
    BalanceteLinha,
    BalanceteResponse,
    DREGrupo,
    DRELinha,
    DREResponse,
    LivroCaixaAgencia,
    LivroCaixaLancamento,
    LivroCaixaResponse,
)

# Mapa tipo → label PT-BR para o DRE
_TIPO_LABEL: dict[str, str] = {
    "receita": "Receitas",
    "custo": "Custos",
    "despesa": "Despesas",
}

# Ordem de exibição no DRE
_TIPO_ORDEM: dict[str, int] = {
    "receita": 0,
    "custo": 1,
    "despesa": 2,
}

# Natureza explícita das contas que pertencem à DRE.
_DRE_NATUREZA: dict[str, str] = {
    "receita": "C",
    "custo": "D",
    "despesa": "D",
}

_ZERO = Decimal("0.00")


def _decimal(value: object) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class RelatoriosService:
    def __init__(self, db: AsyncSession, empresa_id: UUID) -> None:
        self._db = db
        self._empresa_id = empresa_id

    # ─────────────────────────────────────────────── DRE

    async def dre(
        self,
        data_de: datetime | None = None,
        data_ate: datetime | None = None,
    ) -> DREResponse:
        """Demonstração do Resultado do Exercício agrupada por tipo de conta."""
        # Agrega D e C por conta no período
        q = (
            select(
                RegistroContabil.conta_id,
                RegistroContabil.dc,
                func.sum(RegistroContabil.valor).label("total"),
            )
            .where(RegistroContabil.empresa_id == self._empresa_id)
            .where(RegistroContabil.deleted_at.is_(None))
        )
        if data_de:
            q = q.where(RegistroContabil.data_lancamento >= data_de)
        if data_ate:
            q = q.where(RegistroContabil.data_lancamento <= data_ate)
        q = q.group_by(RegistroContabil.conta_id, RegistroContabil.dc)

        rows = (await self._db.execute(q)).all()

        # Organiza por conta_id
        totais: dict[UUID, dict[str, Decimal]] = {}
        for conta_id, dc, total in rows:
            if conta_id not in totais:
                totais[conta_id] = {"D": _ZERO, "C": _ZERO}
            totais[conta_id][dc] += _decimal(total)

        if not totais:
            return DREResponse(
                empresa_id=str(self._empresa_id),
                data_de=data_de,
                data_ate=data_ate,
                grupos=[],
                total_receitas=_ZERO,
                total_custos=_ZERO,
                total_despesas=_ZERO,
                resultado_liquido=_ZERO,
            )

        # Busca metadados das contas
        contas_q = select(PlanoConta).where(
            PlanoConta.empresa_id == self._empresa_id,
            PlanoConta.id.in_(list(totais.keys())),
        )
        contas = {c.id: c for c in (await self._db.execute(contas_q)).scalars().all()}

        # Agrupa por tipo
        grupos_map: dict[str, list[DRELinha]] = {}
        for conta_id, dc_map in totais.items():
            conta = contas.get(conta_id)
            if not conta or conta.tipo not in _DRE_NATUREZA:
                continue
            d = dc_map["D"]
            c = dc_map["C"]
            if _DRE_NATUREZA[conta.tipo] == "C":
                saldo = c - d
            else:
                saldo = d - c

            linha = DRELinha(
                conta_id=str(conta_id),
                codigo=conta.codigo,
                descricao=conta.descricao,
                tipo=conta.tipo,
                debitos=d,
                creditos=c,
                saldo=saldo,
            )
            grupos_map.setdefault(conta.tipo, []).append(linha)

        grupos: list[DREGrupo] = []
        for tipo, linhas in grupos_map.items():
            linhas.sort(key=lambda l: l.codigo)
            total = sum((l.saldo for l in linhas), _ZERO)
            grupos.append(
                DREGrupo(
                    tipo=tipo,
                    label=_TIPO_LABEL.get(tipo, tipo.title()),
                    linhas=linhas,
                    total=total,
                )
            )
        grupos.sort(key=lambda g: _TIPO_ORDEM.get(g.tipo, 99))

        total_receitas = next((g.total for g in grupos if g.tipo == "receita"), _ZERO)
        total_custos = next((g.total for g in grupos if g.tipo == "custo"), _ZERO)
        total_despesas = next((g.total for g in grupos if g.tipo == "despesa"), _ZERO)
        resultado_liquido = total_receitas - total_custos - total_despesas

        return DREResponse(
            empresa_id=str(self._empresa_id),
            data_de=data_de,
            data_ate=data_ate,
            grupos=grupos,
            total_receitas=total_receitas,
            total_custos=total_custos,
            total_despesas=total_despesas,
            resultado_liquido=resultado_liquido,
        )

    # ─────────────────────────────────────────────── Balancete

    async def balancete(
        self,
        data_de: datetime | None = None,
        data_ate: datetime | None = None,
    ) -> BalanceteResponse:
        """Balancete de Verificação — todos os movimentos D/C por conta."""
        q = (
            select(
                RegistroContabil.conta_id,
                RegistroContabil.dc,
                func.sum(RegistroContabil.valor).label("total"),
            )
            .where(RegistroContabil.empresa_id == self._empresa_id)
            .where(RegistroContabil.deleted_at.is_(None))
        )
        if data_de:
            q = q.where(RegistroContabil.data_lancamento >= data_de)
        if data_ate:
            q = q.where(RegistroContabil.data_lancamento <= data_ate)
        q = q.group_by(RegistroContabil.conta_id, RegistroContabil.dc)

        rows = (await self._db.execute(q)).all()

        totais: dict[UUID, dict[str, Decimal]] = {}
        for conta_id, dc, total in rows:
            if conta_id not in totais:
                totais[conta_id] = {"D": _ZERO, "C": _ZERO}
            totais[conta_id][dc] += _decimal(total)

        # Inclui contas ativas mesmo sem movimento e contas removidas que ainda
        # tenham histórico (dados legados anteriores ao bloqueio de remoção).
        contas_q = select(PlanoConta).where(
            PlanoConta.empresa_id == self._empresa_id,
            (PlanoConta.deleted_at.is_(None) | PlanoConta.id.in_(list(totais.keys()))),
        )
        contas = (await self._db.execute(contas_q)).scalars().all()

        linhas: list[BalanceteLinha] = []
        total_debitos = _ZERO
        total_creditos = _ZERO
        total_saldo_devedor = _ZERO
        total_saldo_credor = _ZERO

        for conta in sorted(contas, key=lambda c: c.codigo):
            dc_map = totais.get(conta.id, {"D": _ZERO, "C": _ZERO})
            d = dc_map["D"]
            c = dc_map["C"]
            diff = d - c
            saldo_devedor = diff if diff > 0 else _ZERO
            saldo_credor = (-diff) if diff < 0 else _ZERO

            total_debitos += d
            total_creditos += c
            total_saldo_devedor += saldo_devedor
            total_saldo_credor += saldo_credor

            linhas.append(
                BalanceteLinha(
                    conta_id=str(conta.id),
                    codigo=conta.codigo,
                    descricao=conta.descricao,
                    tipo=conta.tipo,
                    nivel=conta.nivel,
                    debitos=d,
                    creditos=c,
                    saldo_devedor=saldo_devedor,
                    saldo_credor=saldo_credor,
                )
            )

        return BalanceteResponse(
            empresa_id=str(self._empresa_id),
            data_de=data_de,
            data_ate=data_ate,
            linhas=linhas,
            total_debitos=total_debitos,
            total_creditos=total_creditos,
            total_saldo_devedor=total_saldo_devedor,
            total_saldo_credor=total_saldo_credor,
        )

    # ─────────────────────────────────────────────── Livro Caixa

    async def livro_caixa(
        self,
        data_de: datetime | None = None,
        data_ate: datetime | None = None,
    ) -> LivroCaixaResponse:
        """Livro Caixa — movimentação cronológica por agência com saldo acumulado."""
        # Busca agências da empresa
        ag_q = select(AgenciaBancaria).where(
            AgenciaBancaria.empresa_id == self._empresa_id,
            AgenciaBancaria.deleted_at.is_(None),
        )
        agencias = (await self._db.execute(ag_q)).scalars().all()

        if not agencias:
            return LivroCaixaResponse(
                empresa_id=str(self._empresa_id),
                data_de=data_de,
                data_ate=data_ate,
                agencias=[],
            )

        agencia_ids = [agencia.id for agencia in agencias]
        saldos_iniciais: dict[UUID, Decimal] = {
            agencia_id: _ZERO for agencia_id in agencia_ids
        }
        if data_de:
            sq = (
                select(
                    Transacao.agencia_id,
                    Transacao.dc,
                    func.sum(Transacao.valor).label("total"),
                )
                .where(
                    Transacao.empresa_id == self._empresa_id,
                    Transacao.agencia_id.in_(agencia_ids),
                    Transacao.deleted_at.is_(None),
                    Transacao.data < data_de,
                )
                .group_by(Transacao.agencia_id, Transacao.dc)
            )
            for agencia_id, dc, total in (await self._db.execute(sq)).all():
                valor = _decimal(total)
                saldos_iniciais[agencia_id] += valor if dc == "C" else -valor

        tq = select(Transacao).where(
            Transacao.empresa_id == self._empresa_id,
            Transacao.agencia_id.in_(agencia_ids),
            Transacao.deleted_at.is_(None),
        )
        if data_de:
            tq = tq.where(Transacao.data >= data_de)
        if data_ate:
            tq = tq.where(Transacao.data <= data_ate)
        tq = tq.order_by(Transacao.agencia_id, Transacao.data, Transacao.id)
        transacoes_por_agencia: dict[UUID, list[Transacao]] = {
            agencia_id: [] for agencia_id in agencia_ids
        }
        for transacao in (await self._db.execute(tq)).scalars().all():
            transacoes_por_agencia[transacao.agencia_id].append(transacao)

        agencias_resp: list[LivroCaixaAgencia] = []

        for agencia in agencias:
            saldo_inicial = saldos_iniciais[agencia.id]
            transacoes = transacoes_por_agencia[agencia.id]

            lancamentos: list[LivroCaixaLancamento] = []
            saldo_acc = saldo_inicial
            total_debitos = _ZERO
            total_creditos = _ZERO

            for t in transacoes:
                valor = _decimal(t.valor)
                if t.dc == "C":
                    saldo_acc += valor
                    total_creditos += valor
                else:
                    saldo_acc -= valor
                    total_debitos += valor
                lancamentos.append(
                    LivroCaixaLancamento(
                        data=t.data,
                        historico=t.historico,
                        dc=t.dc,
                        valor=valor,
                        saldo_acumulado=saldo_acc,
                    )
                )

            agencias_resp.append(
                LivroCaixaAgencia(
                    agencia_id=str(agencia.id),
                    descricao=agencia.descricao,
                    saldo_inicial=saldo_inicial,
                    lancamentos=lancamentos,
                    saldo_final=saldo_acc,
                    total_debitos=total_debitos,
                    total_creditos=total_creditos,
                )
            )

        return LivroCaixaResponse(
            empresa_id=str(self._empresa_id),
            data_de=data_de,
            data_ate=data_ate,
            agencias=agencias_resp,
        )
