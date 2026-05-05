"""Testes unitários dos schemas de Agências Bancárias."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.agencias import AgenciaCreate, AgenciaUpdate


# ── AgenciaCreate


def test_cria_agencia_basica():
    a = AgenciaCreate(banco_sigla="BRADESCO", agencia="1234", numero="00012345")
    assert a.banco_sigla == "BRADESCO"
    assert a.agencia == "1234"
    assert a.numero == "00012345"
    assert a.digito is None


def test_cria_agencia_com_digito():
    a = AgenciaCreate(banco_sigla="ITAU", agencia="0001", numero="99887", digito="7")
    assert a.digito == "7"


def test_codigo_banco_convertido_para_sigla():
    """Código numérico deve ser convertido para sigla conhecida."""
    a = AgenciaCreate(banco_sigla="237", agencia="1234", numero="00012345")
    assert a.banco_sigla == "BRADESCO"

    a2 = AgenciaCreate(banco_sigla="341", agencia="0001", numero="11111")
    assert a2.banco_sigla == "ITAU"

    a3 = AgenciaCreate(banco_sigla="260", agencia="0001", numero="22222")
    assert a3.banco_sigla == "NU"


def test_banco_desconhecido_mantido():
    """Sigla não mapeada é aceita como está."""
    a = AgenciaCreate(banco_sigla="XPTO", agencia="0001", numero="12345")
    assert a.banco_sigla == "XPTO"


def test_sigla_normalizada_para_maiusculo():
    a = AgenciaCreate(banco_sigla="bradesco", agencia="1234", numero="99999")
    assert a.banco_sigla == "BRADESCO"


def test_agencia_com_letras_rejeita():
    with pytest.raises(ValidationError) as exc_info:
        AgenciaCreate(banco_sigla="BB", agencia="01AB", numero="12345")
    assert "dígitos" in str(exc_info.value)


def test_agencia_vazia_rejeita():
    with pytest.raises(ValidationError):
        AgenciaCreate(banco_sigla="BB", agencia="", numero="12345")


def test_numero_vazio_rejeita():
    with pytest.raises(ValidationError):
        AgenciaCreate(banco_sigla="BB", agencia="1234", numero="")


def test_banco_muito_curto_rejeita():
    with pytest.raises(ValidationError):
        AgenciaCreate(banco_sigla="X", agencia="1234", numero="12345")


def test_digito_espaco_vira_none():
    """Dígito em branco deve ser tratado como None."""
    a = AgenciaCreate(banco_sigla="BB", agencia="1234", numero="12345", digito="  ")
    assert a.digito is None


def test_espacos_removidos():
    a = AgenciaCreate(banco_sigla="ITAU", agencia=" 0001 ", numero=" 99887 ")
    assert a.agencia == "0001"
    assert a.numero == "99887"


# ── AgenciaUpdate


def test_update_parcial_valido():
    u = AgenciaUpdate(banco_sigla="SANTANDER")
    assert u.banco_sigla == "SANTANDER"
    assert u.agencia is None
    assert u.ativa is None


def test_update_vazio_valido():
    """Update sem campos é válido — nenhuma alteração."""
    u = AgenciaUpdate()
    assert u.banco_sigla is None
    assert u.agencia is None


def test_update_ativa_false():
    u = AgenciaUpdate(ativa=False)
    assert u.ativa is False


def test_update_agencia_invalida_rejeita():
    with pytest.raises(ValidationError):
        AgenciaUpdate(agencia="ABC")


@pytest.mark.parametrize("codigo,sigla", [
    ("001", "BB"),
    ("033", "SANTANDER"),
    ("104", "CEF"),
    ("237", "BRADESCO"),
    ("341", "ITAU"),
    ("260", "NU"),
])
def test_mapeamento_codigos_bancos(codigo: str, sigla: str):
    a = AgenciaCreate(banco_sigla=codigo, agencia="0001", numero="12345")
    assert a.banco_sigla == sigla
