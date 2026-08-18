"""Testes unitários — normalização de histórico contábil exibido."""

from __future__ import annotations

import pytest

from src.core.texto import normalizar_historico_contabil


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
