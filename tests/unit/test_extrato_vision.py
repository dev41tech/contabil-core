"""Camada 3 — leitura de PDF de imagem por Vision.

Os quatro extratos do escritório que caem aqui **não são escaneados**: são
exportações do internet banking em que o texto foi desenhado em vez de escrito.
O pdfplumber devolve zero caracteres; a imagem é nítida.

O que torna esta camada aceitável é a **cadeia de saldos**. O prompt pede o saldo
impresso em cada linha junto do valor, e a saída da IA passa pela mesma
conferência de um adaptador determinístico. Se ela pular uma linha, trocar um
sinal ou inventar um valor, o saldo deixa de caminhar e nada é importado.

Estes testes usam um cliente OpenAI falso: nenhuma chamada real, nenhum custo, e
o que está sendo verificado é a conferência — não a IA.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.domain.extrato import pdf_parser
from src.domain.extrato.pdf_parser import PDFParseError, parse_pdf


def _pdf_sem_texto(paginas: int = 1) -> bytes:
    """PDF com páginas em branco: nenhuma camada de texto, como os reais."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for _ in range(paginas):
        doc.new_page(width=595, height=842)
    dados = doc.tobytes()
    doc.close()
    return dados


class _Settings:
    def __init__(self, liberado: bool = True):
        self.openai_enabled = True
        self.allow_financial_data_to_openai = liberado
        self.pdf_parse_timeout_seconds = 60
        self.pdf_max_pages = 120
        self.pdf_max_ai_calls = 10
        self.openai_api_key = type("K", (), {"get_secret_value": lambda self: "sk-teste"})()


class _RespostaFalsa:
    def __init__(self, conteudo: str):
        mensagem = type("M", (), {"content": conteudo})()
        self.choices = [type("C", (), {"message": mensagem})()]


class _ClienteFalso:
    """Devolve uma resposta por página, na ordem, e conta as chamadas."""

    def __init__(self, respostas: list[str]):
        self.respostas = list(respostas)
        self.chamadas = 0
        cliente = self

        class _Completions:
            def create(self, **kwargs):
                cliente.chamadas += 1
                return _RespostaFalsa(cliente.respostas.pop(0))

        self.chat = type("Chat", (), {"completions": _Completions()})()


@pytest.fixture
def openai_falso(monkeypatch):
    """Instala o cliente falso e devolve uma função para programar as respostas."""
    caixa: dict = {}

    def programar(respostas: list[str], liberado: bool = True):
        cliente = _ClienteFalso(respostas)
        caixa["cliente"] = cliente
        monkeypatch.setattr(pdf_parser, "get_settings", lambda: _Settings(liberado))
        import openai

        monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: cliente)
        return cliente

    return programar


# Um extrato de duas linhas cuja cadeia fecha: 1000 → 900 → 950.
_PAGINA_BOA = """
{"saldo_anterior": "1000.00",
 "transacoes": [
   {"data": "05/01/2026", "historico": "PIX ENVIADO EXEMPLO", "valor": -100.00, "saldo": 900.00},
   {"data": "06/01/2026", "historico": "TED RECEBIDA EXEMPLO", "valor": 50.00, "saldo": 950.00}
 ]}
"""


def test_vision_importa_quando_a_cadeia_de_saldos_fecha(openai_falso):
    cliente = openai_falso([_PAGINA_BOA])
    transacoes = parse_pdf(_pdf_sem_texto(1))

    assert cliente.chamadas == 1
    assert [t.valor for t in transacoes] == [Decimal("-100.00"), Decimal("50.00")]
    assert [t.saldo_apos for t in transacoes] == [Decimal("900.00"), Decimal("950.00")]


def test_vision_recusa_quando_a_ia_pula_um_lancamento(openai_falso):
    """O caso que importa: a IA devolve menos linhas do que a página tem.

    Sem a cadeia isso entraria silenciosamente, com o razão a menos.
    """
    pulou = """
    {"saldo_anterior": "1000.00",
     "transacoes": [
       {"data": "06/01/2026", "historico": "TED RECEBIDA EXEMPLO", "valor": 50.00, "saldo": 950.00}
     ]}
    """
    openai_falso([pulou])
    with pytest.raises(PDFParseError, match="não caminha"):
        parse_pdf(_pdf_sem_texto(1))


def test_vision_recusa_quando_a_ia_troca_o_sinal(openai_falso):
    trocado = """
    {"saldo_anterior": "1000.00",
     "transacoes": [
       {"data": "05/01/2026", "historico": "PIX ENVIADO", "valor": 100.00, "saldo": 900.00}
     ]}
    """
    openai_falso([trocado])
    with pytest.raises(PDFParseError, match="não caminha"):
        parse_pdf(_pdf_sem_texto(1))


def test_vision_recusa_quando_o_extrato_nao_traz_saldo(openai_falso):
    """Sem saldo não há como conferir — e importar sem conferência não é seguro."""
    sem_saldo = """
    {"saldo_anterior": null,
     "transacoes": [
       {"data": "05/01/2026", "historico": "PIX ENVIADO", "valor": -100.00, "saldo": null}
     ]}
    """
    openai_falso([sem_saldo])
    with pytest.raises(PDFParseError, match="não pôde ser conferida"):
        parse_pdf(_pdf_sem_texto(1))


def test_vision_junta_as_paginas_e_ordena_do_mais_antigo(openai_falso):
    """Extrato decrescente (Caixa, Santander) sai em ordem cronológica."""
    pagina_recente = """
    {"saldo_anterior": null,
     "transacoes": [
       {"data": "06/01/2026", "historico": "TED RECEBIDA", "valor": 50.00, "saldo": 950.00}
     ]}
    """
    pagina_antiga = """
    {"saldo_anterior": "1000.00",
     "transacoes": [
       {"data": "05/01/2026", "historico": "PIX ENVIADO", "valor": -100.00, "saldo": 900.00}
     ]}
    """
    openai_falso([pagina_recente, pagina_antiga])
    transacoes = parse_pdf(_pdf_sem_texto(2))

    assert [t.data.isoformat() for t in transacoes] == ["2026-01-05", "2026-01-06"]
    assert [t.ordem for t in transacoes] == [0, 1]


def test_vision_nao_chama_a_openai_com_o_consentimento_desligado(openai_falso):
    """`ALLOW_FINANCIAL_DATA_TO_OPENAI` é portão próprio, além da chave."""
    cliente = openai_falso([_PAGINA_BOA], liberado=False)
    with pytest.raises(PDFParseError):
        parse_pdf(_pdf_sem_texto(1))
    assert cliente.chamadas == 0


def test_vision_recusa_pdf_longo_demais_antes_de_gastar_chamada(openai_falso):
    """Uma chamada por página: 12 páginas não cabem no teto de 10."""
    cliente = openai_falso([_PAGINA_BOA] * 12)
    with pytest.raises(PDFParseError, match="período menor"):
        parse_pdf(_pdf_sem_texto(12))
    assert cliente.chamadas == 0
