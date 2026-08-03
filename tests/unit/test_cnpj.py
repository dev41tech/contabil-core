"""Testes unitários — validação de CNPJ por dígito verificador."""

from __future__ import annotations

import pytest

from src.core.cnpj import formatar, somente_digitos, valido


# Os dois únicos CNPJs reais recuperados dos razões do ConcilPro. Servem de
# âncora: se o algoritmo quebrar, estes param de validar.
@pytest.mark.parametrize(
    "cnpj",
    [
        "52.540.787/0001-88",  # AXEL TECNOLOGIA
        "12.810.326/0001-63",  # CARGO TIME
        "52540787000188",      # mesmo CNPJ, sem formatação
    ],
)
def test_cnpj_real_valida(cnpj: str):
    assert valido(cnpj) is True


@pytest.mark.parametrize(
    "cnpj",
    [
        "01.003.007/0001-01",  # placeholder do import_mrcont (idx 1)
        "02.006.014/0001-02",  # placeholder do import_mrcont (idx 2)
        "12.345.678/0001-90",  # DV correto seria 95
        "52.540.787/0001-89",  # um dígito trocado de um CNPJ real
    ],
)
def test_cnpj_com_dv_errado_nao_valida(cnpj: str):
    assert valido(cnpj) is False


@pytest.mark.parametrize("cnpj", ["00.000.000/0000-00", "11.111.111/1111-11"])
def test_digito_repetido_nao_valida(cnpj: str):
    """Sequências de dígito repetido passam no cálculo de DV mas não existem."""
    assert valido(cnpj) is False


@pytest.mark.parametrize("cnpj", ["", "123", "5254078700018", "525407870001888", None])
def test_comprimento_errado_nao_valida(cnpj):
    assert valido(cnpj) is False


def test_somente_digitos_remove_pontuacao():
    assert somente_digitos("52.540.787/0001-88") == "52540787000188"


def test_formatar_aplica_mascara():
    assert formatar("52540787000188") == "52.540.787/0001-88"


def test_formatar_devolve_original_se_nao_tem_14_digitos():
    assert formatar("123") == "123"
