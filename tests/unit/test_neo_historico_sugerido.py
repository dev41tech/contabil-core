"""Testes unitários — geração do histórico sugerido (shadow mode do NEO)."""

from __future__ import annotations

import pytest

from src.domain.neo.engine import gerar_historico_sugerido


@pytest.mark.parametrize(
    ("dc", "razao_social", "numero_nf", "esperado"),
    [
        ("D", "Axel Tecnologia Ltda", None, "PGTO REF AXEL TECNOLOGIA LTDA"),
        ("C", "Axel Tecnologia Ltda", None, "REC REF AXEL TECNOLOGIA LTDA"),
        ("D", "Axel Tecnologia Ltda", "123", "PGTO REF NF 123 - AXEL TECNOLOGIA LTDA"),
        ("C", "Axel Tecnologia Ltda", "456", "REC REF NF 456 - AXEL TECNOLOGIA LTDA"),
    ],
)
def test_gerar_historico_sugerido_templates(dc, razao_social, numero_nf, esperado):
    assert gerar_historico_sugerido(dc, razao_social, numero_nf) == esperado


def test_numero_nf_vazio_equivale_a_ausente():
    assert gerar_historico_sugerido("D", "Fornecedor X", "") == gerar_historico_sugerido(
        "D", "Fornecedor X", None
    )


def test_numero_nf_com_espacos_e_stripado():
    assert gerar_historico_sugerido("D", "Fornecedor X", "  99  ") == (
        "PGTO REF NF 99 - FORNECEDOR X"
    )


def test_resultado_colapsa_espacos_e_fica_maiusculo():
    resultado = gerar_historico_sugerido("D", "  Fornecedor   São José  ", None)
    assert resultado == "PGTO REF FORNECEDOR SÃO JOSÉ"
