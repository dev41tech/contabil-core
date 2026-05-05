"""Testes unitários dos schemas Pydantic — validação de entrada."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.empresas import EmpresaCreate


def test_cnpj_formatado_aceito():
    e = EmpresaCreate(
        razao_social="Empresa Teste",
        cnpj="12.345.678/0001-90",
        regime_tributario="simples_nacional",
    )
    assert e.cnpj == "12.345.678/0001-90"


def test_cnpj_sem_formatacao_normalizado():
    e = EmpresaCreate(
        razao_social="Empresa Teste",
        cnpj="12345678000190",
        regime_tributario="simples_nacional",
    )
    assert e.cnpj == "12.345.678/0001-90"


def test_cnpj_invalido_rejeita():
    with pytest.raises(ValidationError) as exc_info:
        EmpresaCreate(
            razao_social="Empresa Teste",
            cnpj="123",
            regime_tributario="simples_nacional",
        )
    assert "14 dígitos" in str(exc_info.value)


def test_regime_invalido_rejeita():
    with pytest.raises(ValidationError):
        EmpresaCreate(
            razao_social="Empresa Teste",
            cnpj="12.345.678/0001-90",
            regime_tributario="regime_inventado",
        )


def test_razao_social_muito_curta_rejeita():
    with pytest.raises(ValidationError):
        EmpresaCreate(
            razao_social="A",
            cnpj="12.345.678/0001-90",
            regime_tributario="lucro_real",
        )


@pytest.mark.parametrize("regime", ["simples_nacional", "lucro_presumido", "lucro_real"])
def test_todos_regimes_validos(regime: str):
    e = EmpresaCreate(
        razao_social="Empresa Teste LTDA",
        cnpj="12.345.678/0001-90",
        regime_tributario=regime,
    )
    assert e.regime_tributario == regime
