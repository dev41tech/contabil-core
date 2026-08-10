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
    lancamentos = _parse_por_regex(linhas, referencia_ano=2026)

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
    lancamentos = _parse_por_regex(linhas, referencia_ano=2026)

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
    lancamentos = _parse_por_regex(linhas, referencia_ano=2026)

    assert len(lancamentos) == 1
    assert lancamentos[0].descricao == "POSTO IPIRANGA"


def test_parse_por_regex_sem_linhas_reconheciveis_retorna_vazio():
    linhas = ["texto qualquer sem formato de lançamento", "outra linha"]
    assert _parse_por_regex(linhas, referencia_ano=2026) == []


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


def test_validar_total_declarado_nao_bloqueia_quando_nao_ha_total_reconhecivel():
    from src.domain.cartoes.pdf_parser import LancamentoPDF
    from datetime import UTC, datetime

    lancamentos = [
        LancamentoPDF(data_compra=datetime(2026, 3, 5, tzinfo=UTC), descricao="A", valor=Decimal("100.00")),
    ]
    linhas = ["nenhuma linha de total aqui"]

    _validar_total_declarado(linhas, lancamentos)  # não deve levantar
