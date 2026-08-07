"""Serviço de relatórios financeiros: DRE, Balancete e Livro Caixa."""

from __future__ import annotations

from datetime import datetime
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
    "ativo": "Ativo",
    "passivo": "Passivo",
    "patrimonio_liquido": "Patrimônio Líquido",
}

# Ordem de exibição no DRE
_TIPO_ORDEM: dict[str, int] = {
    "receita": 0,
    "custo": 1,
    "despesa": 2,
    "ativo": 3,
    "passivo": 4,
    "patrimonio_liquido": 5,
}


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
        totais: dict[UUID, dict[str, float]] = {}
        for conta_id, dc, total in rows:
            if conta_id not in totais:
                totais[conta_id] = {"D": 0.0, "C": 0.0}
            totais[conta_id][dc] += float(total or 0)

        if not totais:
            return DREResponse(
                empresa_id=str(self._empresa_id),
                data_de=data_de,
                data_ate=data_ate,
                grupos=[],
                total_receitas=0,
                total_custos=0,
                total_despesas=0,
                resultado_liquido=0,
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
            if not conta:
                continue
            d = dc_map["D"]
            c = dc_map["C"]
            # Saldo: receita tem natureza credora → C − D; demais → D − C
            if conta.tipo in ("receita",):
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
            total = sum(l.saldo for l in linhas)
            grupos.append(
                DREGrupo(
                    tipo=tipo,
                    label=_TIPO_LABEL.get(tipo, tipo.title()),
                    linhas=linhas,
                    total=total,
                )
            )
        grupos.sort(key=lambda g: _TIPO_ORDEM.get(g.tipo, 99))

        total_receitas = next((g.total for g in grupos if g.tipo == "receita"), 0.0)
        total_custos = next((g.total for g in grupos if g.tipo == "custo"), 0.0)
        total_despesas = next((g.total for g in grupos if g.tipo == "despesa"), 0.0)
        resultado_liquido = total_receitas - total_custos - total_despesas

        return DREResponse(
            empresa_id=str(self._empresa_id),
            data_de=data_de,
            data_ate=data_ate,
            grupos=grupos,
            total_receitas=round(total_receitas, 2),
            total_custos=round(total_custos, 2),
            total_despesas=round(total_despesas, 2),
            resultado_liquido=round(resultado_liquido, 2),
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

        totais: dict[UUID, dict[str, float]] = {}
        for conta_id, dc, total in rows:
            if conta_id not in totais:
                totais[conta_id] = {"D": 0.0, "C": 0.0}
            totais[conta_id][dc] += float(total or 0)

        # Inclui contas ativas mesmo sem movimento e contas removidas que ainda
        # tenham histórico (dados legados anteriores ao bloqueio de remoção).
        contas_q = select(PlanoConta).where(
            PlanoConta.empresa_id == self._empresa_id,
            (PlanoConta.deleted_at.is_(None) | PlanoConta.id.in_(list(totais.keys()))),
        )
        contas = (await self._db.execute(contas_q)).scalars().all()

        linhas: list[BalanceteLinha] = []
        total_debitos = 0.0
        total_creditos = 0.0
        total_saldo_devedor = 0.0
        total_saldo_credor = 0.0

        for conta in sorted(contas, key=lambda c: c.codigo):
            dc_map = totais.get(conta.id, {"D": 0.0, "C": 0.0})
            d = dc_map["D"]
            c = dc_map["C"]
            diff = d - c
            saldo_devedor = diff if diff > 0 else 0.0
            saldo_credor = (-diff) if diff < 0 else 0.0

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
                    debitos=round(d, 2),
                    creditos=round(c, 2),
                    saldo_devedor=round(saldo_devedor, 2),
                    saldo_credor=round(saldo_credor, 2),
                )
            )

        return BalanceteResponse(
            empresa_id=str(self._empresa_id),
            data_de=data_de,
            data_ate=data_ate,
            linhas=linhas,
            total_debitos=round(total_debitos, 2),
            total_creditos=round(total_creditos, 2),
            total_saldo_devedor=round(total_saldo_devedor, 2),
            total_saldo_credor=round(total_saldo_credor, 2),
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

        agencias_resp: list[LivroCaixaAgencia] = []

        for agencia in agencias:
            # Saldo anterior: tudo antes de data_de
            saldo_inicial = 0.0
            if data_de:
                sq = (
                    select(
                        Transacao.dc,
                        func.sum(Transacao.valor).label("total"),
                    )
                    .where(
                        Transacao.empresa_id == self._empresa_id,
                        Transacao.agencia_id == agencia.id,
                        Transacao.deleted_at.is_(None),
                        Transacao.data < data_de,
                    )
                    .group_by(Transacao.dc)
                )
                for dc, total in (await self._db.execute(sq)).all():
                    val = float(total or 0)
                    saldo_inicial += val if dc == "C" else -val

            # Lançamentos no período
            tq = (
                select(Transacao)
                .where(
                    Transacao.empresa_id == self._empresa_id,
                    Transacao.agencia_id == agencia.id,
                    Transacao.deleted_at.is_(None),
                )
                .order_by(Transacao.data)
            )
            if data_de:
                tq = tq.where(Transacao.data >= data_de)
            if data_ate:
                tq = tq.where(Transacao.data <= data_ate)

            transacoes = (await self._db.execute(tq)).scalars().all()

            lancamentos: list[LivroCaixaLancamento] = []
            saldo_acc = saldo_inicial
            total_debitos = 0.0
            total_creditos = 0.0

            for t in transacoes:
                valor = float(t.valor)
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
                        valor=round(valor, 2),
                        saldo_acumulado=round(saldo_acc, 2),
                    )
                )

            agencias_resp.append(
                LivroCaixaAgencia(
                    agencia_id=str(agencia.id),
                    descricao=agencia.descricao,
                    saldo_inicial=round(saldo_inicial, 2),
                    lancamentos=lancamentos,
                    saldo_final=round(saldo_acc, 2),
                    total_debitos=round(total_debitos, 2),
                    total_creditos=round(total_creditos, 2),
                )
            )

        return LivroCaixaResponse(
            empresa_id=str(self._empresa_id),
            data_de=data_de,
            data_ate=data_ate,
            agencias=agencias_resp,
        )
