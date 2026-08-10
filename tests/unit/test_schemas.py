"""Testes unitários dos schemas Pydantic — validação de entrada."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.v1.setup import SetupRequest
from src.schemas.empresas import EmpresaCreate
from src.schemas.notas import NotaFiscalCreate


def test_cnpj_formatado_aceito():
    e = EmpresaCreate(
        razao_social="Empresa Teste",
        cnpj="12.345.678/0001-95",
        regime_tributario="simples_nacional",
    )
    assert e.cnpj == "12.345.678/0001-95"


def test_cnpj_sem_formatacao_normalizado():
    e = EmpresaCreate(
        razao_social="Empresa Teste",
        cnpj="12345678000195",
        regime_tributario="simples_nacional",
    )
    assert e.cnpj == "12.345.678/0001-95"


def test_cnpj_invalido_rejeita():
    with pytest.raises(ValidationError) as exc_info:
        EmpresaCreate(
            razao_social="Empresa Teste",
            cnpj="123",
            regime_tributario="simples_nacional",
        )
    assert "14 dígitos" in str(exc_info.value)


def test_cnpj_com_digito_verificador_errado_rejeita():
    """14 dígitos não basta: o placeholder da migração tinha o tamanho certo e o DV errado."""
    with pytest.raises(ValidationError) as exc_info:
        EmpresaCreate(
            razao_social="Empresa Teste",
            cnpj="12.345.678/0001-90",  # DV correto seria 95
            regime_tributario="simples_nacional",
        )
    assert "dígitos verificadores" in str(exc_info.value)


def test_cnpj_de_digito_repetido_rejeita():
    """00.000.000/0000-00 passa no cálculo de DV mas não existe na Receita."""
    with pytest.raises(ValidationError):
        EmpresaCreate(
            razao_social="Empresa Teste",
            cnpj="00.000.000/0000-00",
            regime_tributario="simples_nacional",
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda cnpj: NotaFiscalCreate(
            tipo="nfe",
            numero="1",
            cnpj_emitente=cnpj,
            valor="1.00",
            data_emissao="2026-01-01T00:00:00Z",
        ),
        lambda cnpj: SetupRequest(
            tenant_nome="Escritório",
            tenant_cnpj=cnpj,
            admin_nome="Admin",
            admin_email="admin@example.com",
            admin_senha="senha-segura",
        ),
    ],
)
def test_cnpj_com_dv_errado_rejeitado_tambem_em_notas_e_setup(factory):
    with pytest.raises(ValidationError):
        factory("12.345.678/0001-90")


def test_regime_invalido_rejeita():
    with pytest.raises(ValidationError):
        EmpresaCreate(
            razao_social="Empresa Teste",
            cnpj="12.345.678/0001-95",
            regime_tributario="regime_inventado",
        )


def test_razao_social_muito_curta_rejeita():
    with pytest.raises(ValidationError):
        EmpresaCreate(
            razao_social="A",
            cnpj="12.345.678/0001-95",
            regime_tributario="lucro_real",
        )


@pytest.mark.parametrize("regime", ["simples_nacional", "lucro_presumido", "lucro_real"])
def test_todos_regimes_validos(regime: str):
    e = EmpresaCreate(
        razao_social="Empresa Teste LTDA",
        cnpj="12.345.678/0001-95",
        regime_tributario=regime,
    )
    assert e.regime_tributario == regime
