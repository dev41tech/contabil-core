"""Parser de comprovante de pagamento em PDF — busca por rótulo (camada 1)."""

from decimal import Decimal

from src.domain.comprovantes.pdf_parser import _parse_por_regex, _parse_valor


def test_extrai_pix_com_rotulo_e_valor_na_mesma_linha():
    linhas = [
        "Comprovante de Pagamento PIX",
        "Favorecido: JOAO DA SILVA",
        "CPF/CNPJ: 123.456.789-00",
        "Valor Pago: R$ 150,00",
        "Data do Pagamento: 05/01/2026",
    ]
    r = _parse_por_regex(linhas)

    assert r.favorecido == "JOAO DA SILVA"
    assert r.cpf_cnpj == "123.456.789-00"
    assert r.valor_pago == Decimal("150.00")
    assert r.data_pagamento.day == 5
    assert r.data_pagamento.month == 1
    assert r.confianca == "regex"


def test_extrai_com_rotulo_e_valor_em_linhas_separadas():
    """Layout comum de comprovante gerado por app de banco (rótulo\\nvalor)."""
    linhas = [
        "Favorecido",
        "MERCADO CENTRAL LTDA",
        "Valor pago",
        "R$ 89,90",
        "Data de pagamento",
        "10/02/2026",
    ]
    r = _parse_por_regex(linhas)

    assert r.favorecido == "MERCADO CENTRAL LTDA"
    assert r.valor_pago == Decimal("89.90")
    assert r.data_pagamento.day == 10
    assert r.data_pagamento.month == 2


def test_extrai_boleto_distingue_valor_documento_de_valor_pago():
    linhas = [
        "Valor do documento: R$ 500,00",
        "Vencimento: 01/03/2026",
        "Juros: R$ 5,00",
        "Multa: R$ 10,00",
        "Desconto: R$ 0,00",
        "Valor pago: R$ 515,00",
    ]
    r = _parse_por_regex(linhas)

    assert r.valor_documento == Decimal("500.00")
    assert r.valor_pago == Decimal("515.00")
    assert r.juros == Decimal("5.00")
    assert r.multa == Decimal("10.00")
    assert r.desconto == Decimal("0.00")
    assert r.data_vencimento.day == 1
    assert r.data_vencimento.month == 3


def test_extrai_cnpj_do_favorecido():
    linhas = ["Beneficiário: DISTRIBUIDORA XYZ LTDA", "CNPJ: 12.345.678/0001-95"]
    r = _parse_por_regex(linhas)

    assert r.favorecido == "DISTRIBUIDORA XYZ LTDA"
    assert r.cpf_cnpj == "12.345.678/0001-95"


def test_sem_rotulos_reconheciveis_nao_encontra_valor_pago():
    linhas = ["texto qualquer sem formato de comprovante", "outra linha"]
    r = _parse_por_regex(linhas)

    assert r.valor_pago is None
    assert r.favorecido is None


def test_parse_valor_aceita_formato_brasileiro():
    assert _parse_valor("1.234,56") == Decimal("1234.56")
    assert _parse_valor("R$ 89,90") == Decimal("89.90")
