"""Parser de fatura de cartão em PDF — regex genérico e reconciliação de total."""

from decimal import Decimal

import pytest

from src.domain.cartoes.pdf_parser import (
    PDFParseError,
    _parse_por_regex,
    _parse_valor,
    _validar_total_declarado,
)


def test_parse_por_regex_extrai_compra_simples():
    linhas = ["05/03  POSTO IPIRANGA  150,00"]
    lancamentos = _parse_por_regex(linhas, referencia=(2026, 12))

    assert len(lancamentos) == 1
    lanc = lancamentos[0]
    assert lanc.descricao == "POSTO IPIRANGA"
    assert lanc.valor == Decimal("150.00")
    assert lanc.data_compra.day == 5
    assert lanc.data_compra.month == 3
    assert lanc.parcela_atual is None
    assert lanc.parcela_total is None


def test_parse_por_regex_extrai_parcela():
    linhas = ["07/03  UBER *TRIP  02/03  45,90"]
    lancamentos = _parse_por_regex(linhas, referencia=(2026, 12))

    assert len(lancamentos) == 1
    lanc = lancamentos[0]
    assert lanc.descricao == "UBER *TRIP"
    assert lanc.parcela_atual == 2
    assert lanc.parcela_total == 3
    assert lanc.valor == Decimal("45.90")


def test_parse_por_regex_ignora_linhas_de_total_e_cabecalho():
    linhas = [
        "Data      Descrição              Valor",
        "05/03  POSTO IPIRANGA  150,00",
        "Total desta fatura  150,00",
        "Página 1 de 1",
    ]
    lancamentos = _parse_por_regex(linhas, referencia=(2026, 12))

    assert len(lancamentos) == 1
    assert lancamentos[0].descricao == "POSTO IPIRANGA"


def test_parse_por_regex_sem_linhas_reconheciveis_retorna_vazio():
    linhas = ["texto qualquer sem formato de lançamento", "outra linha"]
    assert _parse_por_regex(linhas, referencia=(2026, 12)) == []


def test_parse_valor_aceita_formato_brasileiro():
    assert _parse_valor("1.234,56") == Decimal("1234.56")
    assert _parse_valor("R$ 45,90") == Decimal("45.90")
    assert _parse_valor("-150,00") == Decimal("-150.00")


def test_validar_total_declarado_aceita_quando_soma_bate():
    from src.domain.cartoes.pdf_parser import LancamentoPDF
    from datetime import UTC, datetime

    lancamentos = [
        LancamentoPDF(data_compra=datetime(2026, 3, 5, tzinfo=UTC), descricao="A", valor=Decimal("100.00")),
        LancamentoPDF(data_compra=datetime(2026, 3, 6, tzinfo=UTC), descricao="B", valor=Decimal("50.00")),
    ]
    linhas = ["TOTAL DESTA FATURA  150,00"]

    _validar_total_declarado(linhas, lancamentos)  # não deve levantar


def test_validar_total_declarado_rejeita_quando_soma_diverge():
    from src.domain.cartoes.pdf_parser import LancamentoPDF
    from datetime import UTC, datetime

    lancamentos = [
        LancamentoPDF(data_compra=datetime(2026, 3, 5, tzinfo=UTC), descricao="A", valor=Decimal("100.00")),
    ]
    linhas = ["TOTAL DESTA FATURA  150,00"]

    with pytest.raises(PDFParseError, match="não bate"):
        _validar_total_declarado(linhas, lancamentos)


def test_parse_por_regex_sicredi_mes_abreviado_hora_e_pagamento_negativo():
    """Layout real do Sicredi: mês abreviado PT-BR ("16/dez", não "16/12"),
    hora colada na data ("08:08"), valor com "R$" embutido, e uma linha de
    "Pagamento" com valor negativo ("-R$ 39.249,28") que é a quitação da
    fatura anterior, não uma compra — tem que ser ignorada, não virar uma
    compra positiva de R$ 39 mil."""
    linhas = [
        "16/dez 08:08 Curitiba Presencial Portao R$ 317,93",
        "15/dez 20:55 Pagamento 004691404 -R$ 39.249,28",
        "25/nov 02:12 Presencial Jim Com Plakomaster 02/03 R$ 7.771,00",
    ]
    lancamentos = _parse_por_regex(linhas, referencia=(2026, 1))

    assert len(lancamentos) == 2
    assert lancamentos[0].descricao == "Curitiba Presencial Portao"
    assert lancamentos[0].valor == Decimal("317.93")
    assert lancamentos[0].data_compra.year == 2025  # virada de ano: dez < vencimento jan
    assert lancamentos[0].data_compra.month == 12
    assert lancamentos[0].data_compra.day == 16

    assert lancamentos[1].descricao == "Presencial Jim Com Plakomaster"
    assert lancamentos[1].valor == Decimal("7771.00")
    assert lancamentos[1].parcela_atual == 2
    assert lancamentos[1].parcela_total == 3


def test_referencia_fatura_extrai_ano_e_mes_do_vencimento():
    from src.domain.cartoes.pdf_parser import _referencia_fatura

    linhas = ["Sind Do Com De Veic Vencimento 13/01/2026"]
    assert _referencia_fatura(linhas) == (2026, 1)


def test_referencia_fatura_none_quando_nao_encontra_vencimento():
    from src.domain.cartoes.pdf_parser import _referencia_fatura

    assert _referencia_fatura(["nenhum vencimento aqui"]) is None


def test_validar_total_declarado_nao_bloqueia_quando_nao_ha_total_reconhecivel():
    from src.domain.cartoes.pdf_parser import LancamentoPDF
    from datetime import UTC, datetime

    lancamentos = [
        LancamentoPDF(data_compra=datetime(2026, 3, 5, tzinfo=UTC), descricao="A", valor=Decimal("100.00")),
    ]
    linhas = ["nenhuma linha de total aqui"]

    _validar_total_declarado(linhas, lancamentos)  # não deve levantar
