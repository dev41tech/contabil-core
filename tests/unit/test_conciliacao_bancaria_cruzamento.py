"""Cruzamento razão × extrato — cada camada isolada.

Os casos vêm do razão da conta 2699 da BLD de fev/2025, com valores trocados.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.domain.conciliacao_bancaria.cruzamento import (
    AGRUPADO,
    CONCILIADO,
    DATA_DIFERENTE,
    DUPLICIDADE_EXTRATO,
    DUPLICIDADE_RAZAO,
    SO_EXTRATO,
    SO_RAZAO,
    VALOR_DIVERGENTE,
    Item,
    conciliar,
)

D = Decimal


def r(i, dia, valor, hist="SISPAG FORNECEDORES"):
    return Item(f"r{i}", date(2025, 2, dia), D(valor), hist)


def e(i, dia, valor, hist="Sispag Fornecedores"):
    return Item(f"e{i}", date(2025, 2, dia), D(valor), hist)


def tipos(resultado):
    return sorted(g.tipo for g in resultado.pendencias)


def test_mesmo_dia_mesmo_valor_concilia():
    res = conciliar([r(1, 3, "-1022.91")], [e(1, 3, "-1022.91")])
    assert [g.tipo for g in res.conciliados] == [CONCILIADO]
    assert res.pendencias == []


def test_duplicidade_aponta_o_lancamento_errado_pelo_historico():
    """O banco tem UMA entrada de 520 mil; o razão, duas.

    Pelo valor, qualquer uma casa. O histórico decide: "X ONE EXPRESS" é o par,
    "TRANSFERENCIA ENTRE CONTAS" é a sobra. Casar a primeira que aparece acusava
    o lançamento certo.
    """
    razao = [
        r(1, 24, "520000.00", "TRANSFERENCIA ENTRE CONTAS"),
        r(2, 24, "520000.00", "VALOR REF X ONE EXPRESS LTDA"),
    ]
    extrato = [e(1, 24, "520000.00", "Sispag X ONE EXPRESS LT")]

    res = conciliar(razao, extrato)

    assert res.conciliados[0].razao[0].historico == "VALOR REF X ONE EXPRESS LTDA"
    (dup,) = res.pendencias
    assert dup.tipo == DUPLICIDADE_RAZAO
    assert dup.razao[0].historico == "TRANSFERENCIA ENTRE CONTAS"


def test_duplicidade_no_extrato():
    res = conciliar([r(1, 5, "-150.00")], [e(1, 5, "-150.00"), e(2, 5, "-150.00")])
    assert tipos(res) == [DUPLICIDADE_EXTRATO]


def test_mesmo_valor_com_dias_de_diferenca():
    """Banco que compensa em D+1."""
    res = conciliar([r(1, 10, "-800.00")], [e(1, 11, "-800.00")])
    assert [g.tipo for g in res.conciliados] == [DATA_DIFERENTE]


def test_fora_da_tolerancia_de_dias_nao_concilia():
    res = conciliar([r(1, 1, "-800.00")], [e(1, 9, "-800.00")], tolerancia_dias=3)
    assert tipos(res) == [SO_EXTRATO, SO_RAZAO]


def test_duplicidade_so_depois_da_janela_de_dias():
    """O segundo 520 mil do razão tem par no dia seguinte — não é duplicidade."""
    razao = [r(1, 24, "520000.00"), r(2, 24, "520000.00")]
    extrato = [e(1, 24, "520000.00"), e(2, 25, "520000.00")]
    res = conciliar(razao, extrato)
    assert res.pendencias == []


def test_varios_do_razao_somam_um_do_extrato():
    """O razão abre um Sispag em vários fornecedores; o banco debita o total."""
    razao = [r(1, 6, "-100.00"), r(2, 6, "-250.50"), r(3, 6, "-49.50")]
    res = conciliar(razao, [e(1, 6, "-400.00")])
    (grupo,) = res.conciliados
    assert grupo.tipo == AGRUPADO and len(grupo.razao) == 3
    assert res.pendencias == []


def test_varios_do_extrato_somam_um_do_razao():
    res = conciliar([r(1, 6, "-400.00")], [e(1, 6, "-150.00"), e(2, 6, "-250.00")])
    assert [g.tipo for g in res.conciliados] == [AGRUPADO]


def test_valor_proximo_no_mesmo_dia_e_divergencia():
    res = conciliar([r(1, 6, "-222177.98")], [e(1, 6, "-222185.00")])
    (g,) = res.pendencias
    assert g.tipo == VALOR_DIVERGENTE
    assert g.diferenca == D("7.02")


def test_valor_distante_nao_vira_divergencia():
    """Dois pagamentos diferentes no mesmo dia não são "o mesmo com erro"."""
    res = conciliar([r(1, 6, "-1000.00")], [e(1, 6, "-1400.00")])
    assert tipos(res) == [SO_EXTRATO, SO_RAZAO]


def test_sentidos_opostos_nunca_casam():
    res = conciliar([r(1, 6, "500.00")], [e(1, 6, "-500.00")])
    assert tipos(res) == [SO_EXTRATO, SO_RAZAO]


def test_abertura_do_periodo_fica_fora_das_pendencias():
    """No banco o saldo do mês anterior não é transação — nunca teria par."""
    razao = [r(1, 1, "-213896.43", "RETORNO DE SALDO NEGATIVO - ITAU"), r(2, 3, "-10.00")]
    res = conciliar(razao, [e(1, 3, "-10.00")], periodo_inicio=date(2025, 2, 1))
    assert [a.historico for a in res.abertura] == ["RETORNO DE SALDO NEGATIVO - ITAU"]
    assert res.pendencias == []


def test_so_no_razao_e_so_no_extrato():
    res = conciliar([r(1, 7, "-2279.75")], [e(1, 26, "48000.33", "USO AUTOMATICO CX AVAL")])
    assert tipos(res) == [SO_EXTRATO, SO_RAZAO]
