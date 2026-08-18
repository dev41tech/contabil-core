"""Parser de comprovante de pagamento em PDF/imagem — regex, AI-texto e Vision."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.domain.comprovantes import pdf_parser
from src.domain.comprovantes.pdf_parser import PDFParseError, _parse_por_regex, _parse_valor


def test_extrai_pix_com_rotulo_e_valor_na_mesma_linha():
    linhas = [
        "Comprovante de Pagamento PIX",
        "Favorecido: JOAO DA SILVA",
        "CPF/CNPJ: 123.456.789-00",
        "Valor Pago: R$ 150,00",
        "Data do Pagamento: 05/01/2026",
    ]
    r = _parse_por_regex(linhas)

    assert r.favorecido == "JOAO DA SILVA"
    assert r.cpf_cnpj == "123.456.789-00"
    assert r.valor_pago == Decimal("150.00")
    assert r.data_pagamento.day == 5
    assert r.data_pagamento.month == 1
    assert r.confianca == "regex"


def test_extrai_com_rotulo_e_valor_em_linhas_separadas():
    """Layout comum de comprovante gerado por app de banco (rótulo\\nvalor)."""
    linhas = [
        "Favorecido",
        "MERCADO CENTRAL LTDA",
        "Valor pago",
        "R$ 89,90",
        "Data de pagamento",
        "10/02/2026",
    ]
    r = _parse_por_regex(linhas)

    assert r.favorecido == "MERCADO CENTRAL LTDA"
    assert r.valor_pago == Decimal("89.90")
    assert r.data_pagamento.day == 10
    assert r.data_pagamento.month == 2


def test_extrai_boleto_distingue_valor_documento_de_valor_pago():
    linhas = [
        "Valor do documento: R$ 500,00",
        "Vencimento: 01/03/2026",
        "Juros: R$ 5,00",
        "Multa: R$ 10,00",
        "Desconto: R$ 0,00",
        "Valor pago: R$ 515,00",
    ]
    r = _parse_por_regex(linhas)

    assert r.valor_documento == Decimal("500.00")
    assert r.valor_pago == Decimal("515.00")
    assert r.juros == Decimal("5.00")
    assert r.multa == Decimal("10.00")
    assert r.desconto == Decimal("0.00")
    assert r.data_vencimento.day == 1
    assert r.data_vencimento.month == 3


def test_extrai_cnpj_do_favorecido():
    linhas = ["Beneficiário: DISTRIBUIDORA XYZ LTDA", "CNPJ: 12.345.678/0001-95"]
    r = _parse_por_regex(linhas)

    assert r.favorecido == "DISTRIBUIDORA XYZ LTDA"
    assert r.cpf_cnpj == "12.345.678/0001-95"


def test_sem_rotulos_reconheciveis_nao_encontra_valor_pago():
    linhas = ["texto qualquer sem formato de comprovante", "outra linha"]
    r = _parse_por_regex(linhas)

    assert r.valor_pago is None
    assert r.favorecido is None


def test_parse_valor_aceita_formato_brasileiro():
    assert _parse_valor("1.234,56") == Decimal("1234.56")
    assert _parse_valor("R$ 89,90") == Decimal("89.90")


def test_extrai_comprovante_pix_sicredi():
    """Layout real do Sicredi: "Valor:" sozinho (sem "pago"/"total"), "Nome do
    destinatário" em vez de "Favorecido", "Realizado em" em vez de "Data de
    pagamento". Nenhum desses rótulos batia antes — a extração inteira
    falhava porque valor_pago é o único campo obrigatório."""
    linhas = [
        "Comprovante de Pagamento Pix",
        "JANTAR 70ANO Cris Eventos",
        "Valor: R$ 200,00",
        "Realizado em: 13/05/2026 - 09:33:52",
        "Solicitante: CEZAR AUGUSTUS GUARIENTE",
        "Nome do destinatário: 58.021.701 CRISTIANE LOURENCO",
        "CNPJ do destinatário: 58.021.701/0001-97",
        "Nome do pagador: SIND DO COM DE VEIC",
        "CNPJ do pagador: 76.682.236/0001-17",
    ]
    r = _parse_por_regex(linhas)

    assert r.valor_pago == Decimal("200.00")
    assert r.favorecido == "58.021.701 CRISTIANE LOURENCO"
    assert r.cpf_cnpj == "58.021.701/0001-97"
    assert r.data_pagamento.day == 13
    assert r.data_pagamento.month == 5


# ──────────────────────────────────────────────────────────── Camada 3: Vision

_ITEM_VISION_VALIDO = {
    "favorecido": "JOAO DA SILVA",
    "cpf_cnpj": "123.456.789-00",
    "valor_pago": 150.00,
    "valor_documento": None,
    "data_pagamento": "05/01/2026",
    "data_vencimento": None,
    "juros": None,
    "multa": None,
    "desconto": None,
}


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
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **kwargs):
        return _FakeResponse(self._content)


class _FakeOpenAI:
    def __init__(self, content: str) -> None:
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(content)})()


def _instalar_fake_openai(monkeypatch, content: str) -> None:
    monkeypatch.setattr("openai.OpenAI", lambda api_key=None: _FakeOpenAI(content))


def _pdf_em_branco() -> bytes:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_parse_imagem_via_vision_sucesso(monkeypatch):
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True))
    _instalar_fake_openai(monkeypatch, json.dumps(_ITEM_VISION_VALIDO))

    resultado = pdf_parser.parse_imagem(b"fake-png-bytes", "image/png")

    assert resultado.valor_pago == Decimal("150.00")
    assert resultado.favorecido == "JOAO DA SILVA"
    assert resultado.confianca == "ia"


def test_parse_imagem_com_flag_desligada_gera_erro_claro(monkeypatch):
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: _FakeSettings(ai_ligada=False))

    with pytest.raises(PDFParseError, match="OCR externo desabilitado"):
        pdf_parser.parse_imagem(b"fake-png-bytes", "image/png")


def test_parse_imagem_sem_valor_pago_gera_erro_de_preenchimento_manual(monkeypatch):
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True))
    item_sem_valor = dict(_ITEM_VISION_VALIDO, valor_pago=None)
    _instalar_fake_openai(monkeypatch, json.dumps(item_sem_valor))

    with pytest.raises(PDFParseError, match="Preencha os campos manualmente"):
        pdf_parser.parse_imagem(b"fake-png-bytes", "image/png")


def test_parse_pdf_escaneado_cai_para_vision(monkeypatch):
    """PDF sem camada de texto (escaneado) — antes só existia a mensagem de
    erro; agora tenta Vision (camada 3) antes de desistir."""
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: _FakeSettings(ai_ligada=True))
    _instalar_fake_openai(monkeypatch, json.dumps(_ITEM_VISION_VALIDO))

    resultado = pdf_parser.parse_pdf(_pdf_em_branco())

    assert resultado.valor_pago == Decimal("150.00")
    assert resultado.confianca == "ia"


def test_parse_pdf_escaneado_com_flag_desligada_gera_erro_claro(monkeypatch):
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: _FakeSettings(ai_ligada=False))

    with pytest.raises(PDFParseError, match="OCR externo desabilitado"):
        pdf_parser.parse_pdf(_pdf_em_branco())


# ── Rótulos de valor dos apps de banco (camada 1, sem IA) ────────────────────
#
# Relato do escritório (2026-08-18): "permite arrastar os arquivos, no entanto
# ele não consegue extrair a informação". Boa parte era a camada de regex não
# reconhecer o vocabulário de PIX/TED dos apps — o que jogava a extração na
# camada de IA, que está desligada por padrão (ALLOW_FINANCIAL_DATA_TO_OPENAI).


@pytest.mark.parametrize(
    ("linhas", "esperado"),
    [
        (["Valor", "R$ 1.234,56"], Decimal("1234.56")),
        (["Valor:", "R$ 80,00"], Decimal("80.00")),
        (["Valor do Pix R$ 250,00"], Decimal("250.00")),
        (["Valor da transferência: R$ 90,10"], Decimal("90.10")),
        (["Valor enviado", "R$ 42,00"], Decimal("42.00")),
        (["Valor recebido: R$ 15,75"], Decimal("15.75")),
        (["Valor debitado: R$ 33,00"], Decimal("33.00")),
        (["Total pago: R$ 7,00"], Decimal("7.00")),
        (["Valor a pagar: R$ 61,20"], Decimal("61.20")),
    ],
)
def test_reconhece_rotulos_de_valor_dos_apps_de_banco(linhas, esperado):
    assert _parse_por_regex(linhas).valor_pago == esperado


def test_valor_de_taxa_nao_e_confundido_com_valor_pago():
    """"Valor do IOF" não pode virar o valor do comprovante.

    É o risco de afrouxar o rótulo: um `^valor` solto capturaria a taxa que
    aparece antes do valor real e gravaria R$ 0,38 como pagamento.
    """
    linhas = [
        "Comprovante de transferência",
        "Valor do IOF: R$ 0,38",
        "Valor da tarifa: R$ 2,50",
        "Valor pago: R$ 500,00",
    ]
    assert _parse_por_regex(linhas).valor_pago == Decimal("500.00")


def test_dica_de_ia_desligada_entra_na_mensagem_de_erro(monkeypatch):
    """PDF com texto legível, valor em rótulo desconhecido e IA desligada.

    Sem a dica, falta de configuração e comprovante ilegível dão exatamente a
    mesma mensagem — e o escritório não tem como saber que o que falta é a
    chave da IA, não o arquivo.
    """
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: _FakeSettings(ai_ligada=False))
    monkeypatch.setattr(
        pdf_parser,
        "_extrair_linhas",
        lambda _conteudo, _budget: (["Comprovante", "Quantia transferida R$ 10,00"], 40),
    )

    with pytest.raises(PDFParseError) as exc:
        pdf_parser.parse_pdf(b"%PDF-1.4 qualquer")

    assert "OPENAI_API_KEY" in str(exc.value)
