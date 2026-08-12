"""Parser visual de notas fiscais (DANFe) via PDF/imagem — OCR/Vision."""

from __future__ import annotations

import json

import pytest

from src.domain.notas import visual_parser
from src.domain.notas.visual_parser import VisualParseError, _nota_from_item

_CNPJ_EMITENTE = "12.345.678/0001-95"
_CNPJ_DESTINATARIO = "11.222.333/0001-81"

_ITEM_VALIDO = {
    "tipo": "nfe",
    "numero": "12345",
    "serie": "1",
    "cnpj_emitente": _CNPJ_EMITENTE,
    "nome_emitente": "EMPRESA LTDA",
    "cnpj_destinatario": _CNPJ_DESTINATARIO,
    "valor": 1500.00,
    "data_emissao": "05/01/2026",
    "chave_acesso": "35260112345678000195550010000123451234567890",
}


# ──────────────────────────────────────────────────────────── _nota_from_item

def test_nota_from_item_com_todos_os_campos():
    nota = _nota_from_item(dict(_ITEM_VALIDO))

    assert nota.tipo == "nfe"
    assert nota.numero == "12345"
    assert nota.serie == "1"
    assert nota.cnpj_emitente == _CNPJ_EMITENTE
    assert nota.cnpj_destinatario == _CNPJ_DESTINATARIO
    assert nota.valor == 1500
    assert nota.data_emissao.day == 5
    assert nota.data_emissao.month == 1
    assert nota.chave_acesso == "35260112345678000195550010000123451234567890"
    assert "OCR/Vision" in nota.observacao


def test_nota_from_item_sem_tipo_e_rejeitada():
    item = dict(_ITEM_VALIDO, tipo=None)
    with pytest.raises(ValueError, match="NF-e ou NFS-e"):
        _nota_from_item(item)


def test_nota_from_item_com_cnpj_digito_verificador_invalido_e_rejeitada():
    """OCR pode ler um dígito errado — dígito verificador ruim é motivo de
    rejeição (mais rigoroso que o parser de XML, que não tem assinatura
    criptográfica pra compensar aqui)."""
    item = dict(_ITEM_VALIDO, cnpj_emitente="12.345.678/0001-00")
    with pytest.raises(ValueError, match="CNPJ do emitente"):
        _nota_from_item(item)


def test_nota_from_item_sem_valor_e_rejeitada():
    item = dict(_ITEM_VALIDO, valor=None)
    with pytest.raises(ValueError, match="Valor da nota"):
        _nota_from_item(item)


def test_nota_from_item_sem_data_emissao_e_rejeitada():
    item = dict(_ITEM_VALIDO, data_emissao=None)
    with pytest.raises(ValueError, match="Data de emissão"):
        _nota_from_item(item)


def test_nota_from_item_chave_acesso_incompleta_vira_none_sem_falhar():
    item = dict(_ITEM_VALIDO, chave_acesso="123")
    nota = _nota_from_item(item)
    assert nota.chave_acesso is None


def test_nota_from_item_sem_cnpj_destinatario_ainda_e_valida():
    item = dict(_ITEM_VALIDO, cnpj_destinatario=None)
    nota = _nota_from_item(item)
    assert nota.cnpj_destinatario is None


# ──────────────────────────────────────────────────────────── fakes de OpenAI/settings

class _FakeSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _FakeSettings:
    def __init__(self, *, ai_ligada: bool) -> None:
        self.openai_enabled = ai_ligada
        self.allow_financial_data_to_openai = ai_ligada
        self.openai_api_key = _FakeSecret("sk-test") if ai_ligada else None
        self.pdf_parse_timeout_seconds = 60
        self.pdf_max_pages = 25
        self.pdf_max_ai_calls = 10


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, por_modelo: dict[str, str]) -> None:
        self._por_modelo = por_modelo

    def create(self, *, model: str, **kwargs):
        return _FakeResponse(self._por_modelo[model])


class _FakeChat:
    def __init__(self, por_modelo: dict[str, str]) -> None:
        self.completions = _FakeCompletions(por_modelo)


class _FakeOpenAI:
    def __init__(self, por_modelo: dict[str, str]) -> None:
        self.chat = _FakeChat(por_modelo)


def _instalar_fake_openai(monkeypatch, por_modelo: dict[str, str]) -> None:
    monkeypatch.setattr("openai.OpenAI", lambda api_key=None: _FakeOpenAI(por_modelo))


# ──────────────────────────────────────────────────────────── parse_imagem

def test_parse_imagem_via_vision_sucesso(monkeypatch):
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True)
    )
    _instalar_fake_openai(monkeypatch, {"gpt-4o": json.dumps(_ITEM_VALIDO)})

    nota = visual_parser.parse_imagem(b"fake-png-bytes", "image/png")

    assert nota.numero == "12345"
    assert nota.cnpj_emitente == _CNPJ_EMITENTE


def test_parse_imagem_com_flag_desligada_gera_erro_claro(monkeypatch):
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=False)
    )

    with pytest.raises(VisualParseError, match="OCR externo desabilitado"):
        visual_parser.parse_imagem(b"fake-png-bytes", "image/png")


def test_parse_imagem_com_campos_incompletos_gera_erro_do_campo_faltante(monkeypatch):
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True)
    )
    item_incompleto = dict(_ITEM_VALIDO, valor=None)
    _instalar_fake_openai(monkeypatch, {"gpt-4o": json.dumps(item_incompleto)})

    with pytest.raises(VisualParseError, match="Valor da nota"):
        visual_parser.parse_imagem(b"fake-png-bytes", "image/png")


# ──────────────────────────────────────────────────────────── parse_pdf

def _pdf_com_texto(texto: str) -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), texto)
    data = doc.tobytes()
    doc.close()
    return data


def _pdf_em_branco() -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_parse_pdf_com_camada_de_texto_usa_camada_1_ai_texto(monkeypatch):
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True)
    )
    _instalar_fake_openai(monkeypatch, {"gpt-4o-mini": json.dumps(_ITEM_VALIDO)})

    pdf_bytes = _pdf_com_texto("NOTA FISCAL ELETRONICA - DANFE " * 5)
    nota = visual_parser.parse_pdf(pdf_bytes)

    assert nota.numero == "12345"
    assert nota.cnpj_emitente == _CNPJ_EMITENTE


def test_parse_pdf_sem_camada_de_texto_cai_para_vision(monkeypatch):
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True)
    )
    # camada 1 (gpt-4o-mini) nunca deveria ser chamada aqui — só cadastra gpt-4o
    # pra garantir que, se cair na camada errada, o teste falha com KeyError.
    _instalar_fake_openai(monkeypatch, {"gpt-4o": json.dumps(_ITEM_VALIDO)})

    pdf_bytes = _pdf_em_branco()
    nota = visual_parser.parse_pdf(pdf_bytes)

    assert nota.numero == "12345"


def test_parse_pdf_com_texto_mas_extracao_incompleta_cai_para_vision(monkeypatch):
    """PDF tem texto (camada 1 é tentada), mas a IA de texto não extrai os
    campos obrigatórios — o parser deve cair para Vision (camada 2) em vez
    de falhar direto."""
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True)
    )
    item_incompleto = dict(_ITEM_VALIDO, valor=None)
    _instalar_fake_openai(
        monkeypatch,
        {
            "gpt-4o-mini": json.dumps(item_incompleto),
            "gpt-4o": json.dumps(_ITEM_VALIDO),
        },
    )

    pdf_bytes = _pdf_com_texto("NOTA FISCAL ELETRONICA - DANFE " * 5)
    nota = visual_parser.parse_pdf(pdf_bytes)

    assert nota.numero == "12345"


def test_parse_pdf_com_texto_e_flag_desligada_gera_erro_claro(monkeypatch):
    monkeypatch.setattr(
        visual_parser, "get_settings", lambda: _FakeSettings(ai_ligada=False)
    )

    pdf_bytes = _pdf_com_texto("NOTA FISCAL ELETRONICA - DANFE " * 5)
    with pytest.raises(VisualParseError, match="OCR externo desabilitado"):
        visual_parser.parse_pdf(pdf_bytes)
