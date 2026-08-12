"""Testes unitários dos schemas de Plano de Contas."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from src.schemas.plano_contas import (
    FaixaTipoItem,
    PlanoContaCreate,
    PlanoContaUpdate,
    codigo_para_tupla,
)


# ── PlanoContaCreate — código


@pytest.mark.parametrize("codigo", ["1", "1.1", "1.01", "4.2.3", "1.1.1.1.1", "10.20.30"])
def test_codigos_validos(codigo: str):
    c = PlanoContaCreate(codigo=codigo, descricao="Conta Teste", tipo="ativo")
    assert c.codigo == codigo


@pytest.mark.parametrize("codigo", [
    "A",        # letra
    "1.",       # ponto no fim
    ".1",       # ponto no início
    "1..2",     # ponto duplo
    "1 .2",     # espaço
    "",         # vazio
    "1-2",      # traço
])
def test_codigos_invalidos(codigo: str):
    with pytest.raises(ValidationError):
        PlanoContaCreate(codigo=codigo, descricao="Conta", tipo="ativo")


def test_codigo_com_espacos_e_removido():
    c = PlanoContaCreate(codigo=" 1.1 ", descricao="Conta", tipo="ativo")
    assert c.codigo == "1.1"


# ── PlanoContaCreate — tipo


@pytest.mark.parametrize("tipo", [
    "ativo", "passivo", "patrimonio_liquido",
    "receita", "despesa", "custo", "resultado",
])
def test_tipos_validos(tipo: str):
    c = PlanoContaCreate(codigo="1", descricao="Conta", tipo=tipo)
    assert c.tipo == tipo


def test_tipo_normalizado_para_minusculo():
    c = PlanoContaCreate(codigo="1", descricao="Conta", tipo="ATIVO")
    assert c.tipo == "ativo"


def test_tipo_invalido_rejeita():
    with pytest.raises(ValidationError) as exc_info:
        PlanoContaCreate(codigo="1", descricao="Conta", tipo="invalido")
    assert "Tipo inválido" in str(exc_info.value)


@pytest.mark.parametrize("tipo", ["PL", "pl", " Pl "])
def test_tipo_pl_resolve_para_patrimonio_liquido(tipo: str):
    """"PL" é a abreviação usual de patrimônio líquido nos razões importados."""
    c = PlanoContaCreate(codigo="1", descricao="Conta", tipo=tipo)
    assert c.tipo == "patrimonio_liquido"


# ── PlanoContaCreate — descricao


def test_descricao_muito_curta_rejeita():
    with pytest.raises(ValidationError):
        PlanoContaCreate(codigo="1", descricao="X", tipo="ativo")


def test_descricao_stripped():
    c = PlanoContaCreate(codigo="1", descricao="  Ativo Circulante  ", tipo="ativo")
    assert c.descricao == "Ativo Circulante"


# ── PlanoContaCreate — pai_id


def test_pai_id_none_por_padrao():
    c = PlanoContaCreate(codigo="1", descricao="Ativo", tipo="ativo")
    assert c.pai_id is None


def test_pai_id_uuid_valido():
    pid = uuid.uuid4()
    c = PlanoContaCreate(codigo="1.1", descricao="Ativo Circulante", tipo="ativo", pai_id=pid)
    assert c.pai_id == pid


# ── PlanoContaUpdate


def test_update_vazio_valido():
    u = PlanoContaUpdate()
    assert u.descricao is None
    assert u.tipo is None


def test_update_apenas_descricao():
    u = PlanoContaUpdate(descricao="Nova Descrição")
    assert u.descricao == "Nova Descrição"
    assert u.tipo is None


def test_update_tipo_invalido_rejeita():
    with pytest.raises(ValidationError):
        PlanoContaUpdate(tipo="inexistente")


def test_update_tipo_pl_resolve_para_patrimonio_liquido():
    u = PlanoContaUpdate(tipo="PL")
    assert u.tipo == "patrimonio_liquido"


def test_update_descricao_stripped():
    u = PlanoContaUpdate(descricao="   Conta Atualizada   ")
    assert u.descricao == "Conta Atualizada"


# ── Validação de hierarquia de código (service, não schema)
# Testados como chamadas diretas ao método do service para não precisar de banco


def test_assert_codigo_filho_valido_ok():
    from src.domain.plano_contas.service import PlanoContaService
    # Método síncrono — pode ser chamado diretamente
    svc = PlanoContaService.__new__(PlanoContaService)
    svc._assert_codigo_filho_valido("1", "1.1")  # não deve lançar


def test_assert_codigo_filho_invalido_lanca():
    from src.core.errors import ValidationError as AppValidationError
    from src.domain.plano_contas.service import PlanoContaService

    svc = PlanoContaService.__new__(PlanoContaService)
    with pytest.raises(AppValidationError):
        svc._assert_codigo_filho_valido("1", "2.1")  # 2.1 não é filho de 1


def test_assert_codigo_filho_mesmo_codigo_lanca():
    from src.core.errors import ValidationError as AppValidationError
    from src.domain.plano_contas.service import PlanoContaService

    svc = PlanoContaService.__new__(PlanoContaService)
    with pytest.raises(AppValidationError):
        svc._assert_codigo_filho_valido("1", "1")  # igual ao pai não é filho


def test_assert_nivel_maximo_lanca():
    from src.core.errors import ValidationError as AppValidationError
    from src.domain.plano_contas.service import PlanoContaService, _NIVEL_MAXIMO

    svc = PlanoContaService.__new__(PlanoContaService)
    codigo_no_limite = ".".join(["1"] * _NIVEL_MAXIMO)  # ex: "1.1.1.1.1"
    with pytest.raises(AppValidationError):
        svc._assert_nivel_valido(codigo_no_limite)


def test_assert_nivel_abaixo_limite_ok():
    from src.domain.plano_contas.service import PlanoContaService, _NIVEL_MAXIMO

    svc = PlanoContaService.__new__(PlanoContaService)
    codigo_abaixo = ".".join(["1"] * (_NIVEL_MAXIMO - 1))  # ex: "1.1.1.1"
    svc._assert_nivel_valido(codigo_abaixo)  # não deve lançar


# ── codigo_para_tupla / FaixaTipoItem (faixas de classificação) ──────────────


def test_codigo_para_tupla_compara_numericamente_nao_como_texto():
    # "3.10" > "3.9" numericamente, mas seria o contrário como string
    assert codigo_para_tupla("3.10") > codigo_para_tupla("3.9")
    assert codigo_para_tupla("3.2.1.04.001") < codigo_para_tupla("3.3")


def test_faixa_tipo_item_aceita_pl_como_alias():
    f = FaixaTipoItem(tipo="PL", codigo_de="2.3", codigo_ate="2.3.999999")
    assert f.tipo == "patrimonio_liquido"


def test_faixa_tipo_item_tipo_invalido_rejeita():
    with pytest.raises(ValidationError):
        FaixaTipoItem(tipo="invalido", codigo_de="1", codigo_ate="1.999999")


@pytest.mark.parametrize("codigo", ["", "1.", ".1", "1..2", "abc"])
def test_faixa_tipo_item_codigo_invalido_rejeita(codigo):
    with pytest.raises(ValidationError):
        FaixaTipoItem(tipo="ativo", codigo_de=codigo, codigo_ate="1.999999")


def test_faixa_tipo_item_de_maior_que_ate_rejeita():
    with pytest.raises(ValidationError):
        FaixaTipoItem(tipo="ativo", codigo_de="1.5", codigo_ate="1.2")


def test_faixa_tipo_item_de_igual_ate_ok():
    f = FaixaTipoItem(tipo="ativo", codigo_de="1", codigo_ate="1")
    assert f.codigo_de == f.codigo_ate == "1"
