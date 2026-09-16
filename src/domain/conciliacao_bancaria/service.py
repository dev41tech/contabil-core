"""Conciliação de uma conta bancária: razão enviado × extrato já importado.

Só relata. Nenhum lançamento é criado, estornado ou marcado — a análise e os
ajustes ficam com o contador.

ANTES DE CRUZAR, TRÊS CONFERÊNCIAS

Cruzar o razão errado com o extrato certo produz um relatório inteiro de
pendências falsas, com cara de verdadeiro. Por isso:

1. o CNPJ do razão tem de ser o da empresa;
2. a conta do razão tem de ser a conta contábil vinculada a esta conta bancária
   (quando houver vínculo) — senão é o razão de outro banco;
3. o extrato importado tem de cobrir o período do razão. Sem extrato no fim do
   mês, cada lançamento daqueles dias vira "só no razão", e o problema é a
   importação que faltou, não a contabilidade. Isso sai como aviso.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import NotFoundError, ValidationError
from src.db.models import AgenciaBancaria, Empresa, PlanoConta, Transacao
from src.domain.conciliacao_bancaria.cruzamento import (
    DUPLICIDADE_EXTRATO,
    DUPLICIDADE_RAZAO,
    Item,
    conciliar,
)
from src.domain.conciliacao_bancaria.razao import RazaoConta
from src.schemas.conciliacao_bancaria import (
    GrupoConciliacao,
    LinhaExtrato,
    LinhaRazao,
    RelatorioConciliacao,
    ResumoConciliacao,
)

# Dias sem extrato nas pontas do período antes de avisar. Fim de semana e
# feriado deixam alguns dias sem movimento de verdade.
_FOLGA_COBERTURA = timedelta(days=4)


def _so_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


async def conciliar_razao_extrato(
    db: AsyncSession, *, empresa_id: UUID, agencia_id: UUID, razao: RazaoConta
) -> RelatorioConciliacao:
    empresa = await db.get(Empresa, empresa_id)
    agencia = (
        await db.execute(
            select(AgenciaBancaria).where(
                AgenciaBancaria.id == agencia_id,
                AgenciaBancaria.empresa_id == empresa_id,
                AgenciaBancaria.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if agencia is None:
        raise NotFoundError(message="Conta bancária não encontrada nesta empresa.")

    nome_conta = f"{agencia.banco_sigla} ag. {agencia.agencia} c/c {agencia.numero}"
    conta_razao = f"{razao.conta_codigo} - {razao.conta_classificacao} {razao.conta_descricao}".strip()
    avisos: list[str] = []

    if empresa and razao.cnpj and _so_digitos(empresa.cnpj) != razao.cnpj:
        raise ValidationError(
            message=(
                f"O razão é de outra empresa (CNPJ {razao.cnpj}, {razao.empresa}). "
                "Confira se o arquivo e a empresa selecionada são os mesmos."
            )
        )

    if agencia.conta_contabil_id:
        conta = await db.get(PlanoConta, agencia.conta_contabil_id)
        if conta is not None:
            mesmo_codigo = conta.conta_numero is not None and str(conta.conta_numero) == razao.conta_codigo
            mesma_classificacao = conta.codigo == razao.conta_classificacao
            if not (mesmo_codigo or mesma_classificacao):
                raise ValidationError(
                    message=(
                        f"O razão enviado é da conta {conta_razao}, mas a conta bancária "
                        f"{nome_conta} está vinculada à conta contábil "
                        f"{conta.conta_numero or ''} {conta.codigo} {conta.descricao}. "
                        "Envie o razão desta conta, ou escolha a conta bancária certa."
                    )
                )
    else:
        avisos.append(
            f"A conta bancária {nome_conta} não tem conta contábil vinculada, então não deu "
            f"para confirmar que o razão ({conta_razao}) é desta conta."
        )

    if razao.periodo_inicio is None or razao.periodo_fim is None:
        raise ValidationError(message="Não encontrei o período no cabeçalho do razão.")

    transacoes = (
        await db.execute(
            select(Transacao)
            .where(
                Transacao.empresa_id == empresa_id,
                Transacao.agencia_id == agencia_id,
                Transacao.deleted_at.is_(None),
                Transacao.data >= razao.periodo_inicio,
                Transacao.data <= razao.periodo_fim,
            )
            .order_by(Transacao.data, Transacao.ordem)
        )
    ).scalars().all()

    if not transacoes:
        raise ValidationError(
            message=(
                f"Não há extrato importado para {nome_conta} entre "
                f"{razao.periodo_inicio:%d/%m/%Y} e {razao.periodo_fim:%d/%m/%Y}. "
                "Importe o extrato do período antes de conciliar."
            )
        )

    primeira, ultima = transacoes[0].data, transacoes[-1].data
    if primeira - razao.periodo_inicio > _FOLGA_COBERTURA or razao.periodo_fim - ultima > _FOLGA_COBERTURA:
        avisos.append(
            f"O extrato importado vai de {primeira:%d/%m/%Y} a {ultima:%d/%m/%Y}, e o razão de "
            f"{razao.periodo_inicio:%d/%m/%Y} a {razao.periodo_fim:%d/%m/%Y}. Lançamentos fora "
            "desse intervalo vão aparecer como \"só no razão\" por falta de extrato, não por erro."
        )

    itens_r = {
        f"r{i}": (Item(f"r{i}", l.data, l.valor, l.historico), l)
        for i, l in enumerate(razao.lancamentos)
    }
    itens_e = {
        str(t.id): (Item(str(t.id), t.data, Decimal(t.valor) * (1 if t.dc == "C" else -1), t.historico), t)
        for t in transacoes
    }
    resultado = conciliar(
        [i for i, _ in itens_r.values()],
        [i for i, _ in itens_e.values()],
        periodo_inicio=razao.periodo_inicio,
    )

    def _linha_r(item: Item) -> LinhaRazao:
        l = itens_r[item.id][1]
        return LinhaRazao(data=l.data, valor=l.valor, historico=l.historico,
                          lote=l.lote, contrapartida=l.contrapartida)

    def _linha_e(item: Item) -> LinhaExtrato:
        return LinhaExtrato(transacao_id=UUID(item.id), data=item.data,
                            valor=item.valor, historico=item.historico)

    movimento_razao = sum((i.valor for i, _ in itens_r.values()), Decimal("0")) - sum(
        (a.valor for a in resultado.abertura), Decimal("0")
    )
    movimento_extrato = sum((i.valor for i, _ in itens_e.values()), Decimal("0"))
    # Na duplicidade, o lado repetido é pendência; o outro já está conciliado e
    # não pode ser contado duas vezes.
    explicado = sum(
        (
            sum((i.valor for i in g.razao), Decimal("0")) * (g.tipo != DUPLICIDADE_EXTRATO)
            - sum((i.valor for i in g.extrato), Decimal("0")) * (g.tipo != DUPLICIDADE_RAZAO)
            for g in resultado.pendencias
        ),
        Decimal("0"),
    )
    diferenca = movimento_razao - movimento_extrato

    return RelatorioConciliacao(
        resumo=ResumoConciliacao(
            empresa=razao.empresa,
            conta_razao=conta_razao,
            conta_bancaria=nome_conta,
            periodo_inicio=razao.periodo_inicio,
            periodo_fim=razao.periodo_fim,
            lancamentos_razao=len(itens_r),
            lancamentos_extrato=len(itens_e),
            conciliados=len(resultado.conciliados),
            pendencias=len(resultado.pendencias),
            movimento_razao=movimento_razao,
            movimento_extrato=movimento_extrato,
            diferenca=diferenca,
            diferenca_explicada=explicado == diferenca,
            abertura=[_linha_r(a) for a in resultado.abertura],
        ),
        avisos=avisos,
        conciliados_por_tipo=dict(Counter(g.tipo for g in resultado.conciliados)),
        pendencias=[
            GrupoConciliacao(
                tipo=g.tipo,
                razao=[_linha_r(i) for i in g.razao],
                extrato=[_linha_e(i) for i in g.extrato],
                diferenca=g.diferenca,
            )
            for g in resultado.pendencias
        ],
    )
