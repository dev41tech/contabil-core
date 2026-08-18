"""Testes unitários — normalização de histórico contábil exibido."""

from __future__ import annotations

import pytest

from src.core.texto import (
    normalizar_historico_contabil,
    normalizar_para_match,
    remover_acentos,
    tokens_para_match,
)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("pgto ref fornecedor", "PGTO REF FORNECEDOR"),
        ("  Pgto   ref\nFornecedor São José  ", "PGTO REF FORNECEDOR SÃO JOSÉ"),
        ("TED\tRECEBIDA", "TED RECEBIDA"),
        ("já em maiúsculas", "JÁ EM MAIÚSCULAS"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalizar_historico_contabil(entrada: str, esperado: str):
    assert normalizar_historico_contabil(entrada) == esperado


def test_preserva_acentos():
    assert normalizar_historico_contabil("informação") == "INFORMAÇÃO"


# ── Normalização para matching de regra ──────────────────────────────────────


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("TARIFA", "tarifa"),
        ("Tarifa", "tarifa"),
        ("TARIFA COM LIQUIDAÇÃO", "tarifa com liquidacao"),
        ("TARIFA  COM/LIQUIDAÇÃO", "tarifa com liquidacao"),
        ("TARIFA-BANCARIA", "tarifa bancaria"),
        ("  PIX ENVIADO  ", "pix enviado"),
        ("DOC 12.345", "doc 12 345"),
        ("", ""),
    ],
)
def test_normalizar_para_match(entrada: str, esperado: str):
    assert normalizar_para_match(entrada) == esperado


def test_normalizar_para_match_junta_as_variacoes_do_banco():
    """As três formas que o extrato traz colapsam na mesma forma canônica."""
    formas = ["TARIFA COM LIQUIDAÇÃO", "Tarifa com liquidacao", "TARIFA/COM/LIQUIDACAO"]
    assert len({normalizar_para_match(f) for f in formas}) == 1


def test_remover_acentos_preserva_a_letra():
    assert remover_acentos("LIQUIDAÇÃO São José") == "LIQUIDACAO Sao Jose"


def test_tokens_para_match():
    assert tokens_para_match("TARIFA COM R LIQUIDAÇÃO") == [
        "tarifa",
        "com",
        "r",
        "liquidacao",
    ]
    assert tokens_para_match("   ") == []
