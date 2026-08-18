"""Testes unitários — validação dos schemas de Contrapartes."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from src.schemas.contrapartes import ContraparteCreate


def _payload(**over) -> dict:
    payload = {
        "tipo": "fornecedor",
        "documento": "52.540.787/0001-88",
        "razao_social": "Axel Tecnologia Ltda",
        "conta_contabil_id": str(uuid.uuid4()),
    }
    payload.update(over)
    return payload


def test_documento_cnpj_normaliza_para_digitos():
    c = ContraparteCreate(**_payload())
    assert c.documento == "52540787000188"


def test_documento_cpf_aceito():
    c = ContraparteCreate(**_payload(documento="123.456.789-09"))
    assert c.documento == "12345678909"


@pytest.mark.parametrize("documento", ["123", "1234567890123456", ""])
def test_documento_com_tamanho_invalido_rejeita(documento):
    with pytest.raises(ValidationError):
        ContraparteCreate(**_payload(documento=documento))


def test_tipo_invalido_rejeita():
    with pytest.raises(ValidationError):
        ContraparteCreate(**_payload(tipo="parceiro"))


@pytest.mark.parametrize("tipo", ["fornecedor", "cliente", "ambos", "FORNECEDOR"])
def test_tipo_valido_aceita_case_insensitive(tipo):
    c = ContraparteCreate(**_payload(tipo=tipo))
    assert c.tipo == tipo.lower()


def test_razao_social_e_nome_fantasia_sao_stripados():
    c = ContraparteCreate(**_payload(razao_social="  Axel Tecnologia  ", nome_fantasia="  Axel  "))
    assert c.razao_social == "Axel Tecnologia"
    assert c.nome_fantasia == "Axel"
