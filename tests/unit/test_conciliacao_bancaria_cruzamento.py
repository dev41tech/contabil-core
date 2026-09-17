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


# ── aplicação automática que o extrato não traz ─────────────────────────────
#
# O extrato do internet banking do Itaú traz os resgates e nunca as aplicações.
# No razão da conta banco as duas espécies existem (contrapartida na conta de
# aplicação), e cada aplicação virava "só no razão" — pendência falsa.

def test_aplicacao_que_o_extrato_nao_traz_sai_das_pendencias():
    razao = [
        r(1, 3, "243239.41", "RESGATE DE APLICAÇÃO AUTOMÁTICA"),
        r(2, 5, "-160208.40", "OPERAÇÃO DE APLICAÇÃO AUTOMÁTICA"),
    ]
    extrato = [e(1, 3, "243239.41", "RES APLIC AUT MAIS")]

    res = conciliar(razao, extrato)

    assert [g.tipo for g in res.conciliados] == [CONCILIADO]
    assert res.pendencias == []
    assert [i.historico for i in res.aplicacao_sem_extrato] == ["OPERAÇÃO DE APLICAÇÃO AUTOMÁTICA"]


def test_especie_que_o_extrato_traz_continua_sendo_conferida():
    """Com UM resgate no extrato, o resgate sem par é pendência de verdade."""
    razao = [
        r(1, 3, "243239.41", "RESGATE DE APLICAÇÃO AUTOMÁTICA"),
        r(2, 6, "2801856.73", "RESGATE DE APLICAÇÃO AUTOMÁTICA"),
    ]
    extrato = [e(1, 3, "243239.41", "Res Aplic Aut Mais")]

    res = conciliar(razao, extrato)

    assert tipos(res) == [SO_RAZAO]
    assert res.aplicacao_sem_extrato == []


def test_aplicacao_sem_extrato_nao_casa_com_outro_lancamento_de_mesmo_valor():
    """Separada antes das camadas: não pode virar par de um PIX de mesmo valor."""
    razao = [r(1, 4, "1040.30", "RESGATE DE APLICAÇÃO AUTOMÁTICA")]
    extrato = [e(1, 5, "1040.30", "PIX RECEBIDO CLIENTE")]

    res = conciliar(razao, extrato)

    assert res.conciliados == []
    assert [i.id for i in res.aplicacao_sem_extrato] == ["r1"]
    assert tipos(res) == [SO_EXTRATO]


def test_rendimento_e_conferido_a_parte_do_resgate():
    razao = [
        r(1, 3, "0.08", "REND PAGO APLIC AUT MAIS"),
        r(2, 3, "243239.41", "RESGATE DE APLICAÇÃO AUTOMÁTICA"),
    ]
    extrato = [e(1, 3, "243239.41", "RES APLIC AUT MAIS")]

    res = conciliar(razao, extrato)

    assert [i.historico for i in res.aplicacao_sem_extrato] == ["REND PAGO APLIC AUT MAIS"]
    assert res.pendencias == []


def test_nome_com_rend_no_meio_nao_e_rendimento():
    """"MARENDA" contém REND — medido no razão da 2699."""
    res = conciliar([r(1, 3, "-500.00", "PGTO JAIME CARLOS MARENDA")], [])
    assert res.aplicacao_sem_extrato == []
    assert tipos(res) == [SO_RAZAO]
