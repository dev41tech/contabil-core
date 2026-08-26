"""Testes unitários — CPF/CNPJ impresso na linha do extrato.

As linhas vêm de testes reais do contador na SINCOPEÇAS (26/08/2026), onde o
fornecedor estava cadastrado com o CNPJ exato e a transação ficava pendente.
"""

from __future__ import annotations

import pytest

from src.domain.neo.documento_no_historico import documentos_no_historico


@pytest.mark.parametrize(
    "historico,esperado",
    [
        # Os dois casos que o contador testou e não classificaram.
        (
            "PAGAMENTO PIX 09033833000123 PERFORMANCE ENGENHA PIX_DEB",
            ["09033833000123"],
        ),
        (
            "LIQUIDACAO BOLETO SICREDI 31052957000105 041 CON 252080163",
            ["31052957000105"],
        ),
        # Convênio: o CNPJ é o do convenente, e vem no meio de valores.
        (
            "20/02/2026 DEBITO CONVENIOS 76417005000186 PMCURIT-G -1.788,13 -70.254,20",
            ["76417005000186"],
        ),
        # Forma pontuada.
        ("PIX 09.033.833/0001-23 PERFORMANCE", ["09033833000123"]),
        ("TED 123.456.789-09 JOAO", ["12345678909"]),
        # O mesmo documento duas vezes não é ambiguidade.
        (
            "PIX 09.033.833/0001-23 PERFORMANCE 09033833000123",
            ["09033833000123"],
        ),
        # Lixo numérico que NÃO pode virar documento.
        ("TARIFA BAIXA DE TITULOS COB000004", []),
        ("LIQUIDACAO BOLETO 252080163 SICREDI 251009131", []),
        # Código de barras de 24 dígitos: um pedaço dele não é CNPJ.
        ("BOLETO 123456789012345678901234 NOSSO NUMERO", []),
        ("", []),
    ],
)
def test_documentos_no_historico(historico: str, esperado: list[str]):
    assert documentos_no_historico(historico) == esperado


def test_dois_documentos_na_mesma_linha_saem_na_ordem_de_leitura():
    """O caso do boleto Sicredi: o CNPJ do banco e o do cedente na mesma linha.

    Quem chama precisa dos dois para perceber a ambiguidade — devolver só o
    primeiro esconderia o conflito e classificaria pelo banco.
    """
    historico = "LIQUIDACAO BOLETO SICREDI 07070495000174 CED 31052957000105"
    assert documentos_no_historico(historico) == [
        "07070495000174",
        "31052957000105",
    ]


def test_historico_none_nao_quebra():
    assert documentos_no_historico(None) == []
