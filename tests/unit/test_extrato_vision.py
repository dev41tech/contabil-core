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

import base64
import struct
import threading
from decimal import Decimal

import pytest

from src.domain.extrato import pdf_parser
from src.domain.extrato.pdf_parser import PDFParseError, parse_pdf

_LARGURA_BASE = 595


def _pdf_sem_texto(paginas: int = 1) -> bytes:
    """PDF com páginas em branco: nenhuma camada de texto, como os reais.

    Cada página nasce um ponto mais larga que a anterior. Isso não muda nada no
    que está sendo testado e resolve um problema do teste: como as páginas são
    lidas em paralelo, a ordem das chamadas não diz mais de qual página cada uma
    veio — a largura do PNG diz.
    """
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for numero in range(paginas):
        doc.new_page(width=_LARGURA_BASE + numero, height=842)
    dados = doc.tobytes()
    doc.close()
    return dados


def _pagina_da_imagem(url: str, dpi: int = 150) -> int:
    """Devolve o número da página lendo a largura no cabeçalho IHDR do PNG."""
    png = base64.b64decode(url.split(",", 1)[1])
    largura_px = struct.unpack(">I", png[16:20])[0]
    return round(largura_px * 72 / dpi) - _LARGURA_BASE


class _Settings:
    def __init__(self, liberado: bool = True):
        self.openai_enabled = True
        self.allow_financial_data_to_openai = liberado
        self.pdf_parse_timeout_seconds = 60
        self.pdf_max_pages = 120
        self.pdf_max_ai_calls = 10
        self.pdf_vision_timeout_seconds = 300
        self.pdf_vision_call_timeout_seconds = 120
        self.pdf_vision_concurrency = 6
        self.openai_api_key = type("K", (), {"get_secret_value": lambda self: "sk-teste"})()


class _RespostaFalsa:
    def __init__(self, conteudo: str, finish_reason: str = "stop"):
        mensagem = type("M", (), {"content": conteudo})()
        self.choices = [
            type("C", (), {"message": mensagem, "finish_reason": finish_reason})()
        ]


class _ClienteFalso:
    """Devolve a resposta da página que a imagem identifica.

    Endereçar por página, e não pela ordem de chegada, é o que mantém estes
    testes determinísticos agora que as páginas são lidas em paralelo.
    """

    def __init__(self, respostas: list[str], erro_na_pagina: int | None = None,
                 truncar_na_pagina: int | None = None,
                 avanco_do_relogio: float = 0.0, relogio: _Relogio | None = None,
                 barreira: threading.Barrier | None = None):
        self.respostas = list(respostas)
        self.chamadas = 0
        # A camada 2 (texto) e a 3 (imagem) usam o MESMO cliente. Contar só
        # chamadas não distingue "não gastou IA" de "gastou a IA barata", que é
        # exatamente a diferença que os testes do portão de legibilidade medem.
        self.chamadas_com_imagem = 0
        self.timeouts: list[float] = []
        self.simultaneas_maximo = 0
        self._em_voo = 0
        self._trava = threading.Lock()
        cliente = self

        class _Completions:
            def create(self, **kwargs):
                with cliente._trava:
                    cliente.chamadas += 1
                    cliente._em_voo += 1
                    cliente.simultaneas_maximo = max(
                        cliente.simultaneas_maximo, cliente._em_voo
                    )
                    cliente.timeouts.append(kwargs.get("timeout"))
                    if relogio is not None:
                        relogio.avancar(avanco_do_relogio)
                try:
                    if barreira is not None:
                        # Só passa quando TODAS as páginas chegarem aqui: numa
                        # leitura em fila a barreira estoura e o teste falha,
                        # em vez de passar por acaso de agendamento.
                        barreira.wait()
                    partes = kwargs["messages"][1]["content"]
                    if not isinstance(partes, list):
                        return _RespostaFalsa("[]")      # camada 2: texto puro
                    with cliente._trava:
                        cliente.chamadas_com_imagem += 1
                    pagina = _pagina_da_imagem(partes[0]["image_url"]["url"])
                    if pagina == erro_na_pagina:
                        raise RuntimeError("APITimeoutError simulado")
                    if pagina == truncar_na_pagina:
                        return _RespostaFalsa(
                            cliente.respostas[pagina][:60], finish_reason="length"
                        )
                    return _RespostaFalsa(cliente.respostas[pagina])
                finally:
                    with cliente._trava:
                        cliente._em_voo -= 1

        self.chat = type("Chat", (), {"completions": _Completions()})()


class _Relogio:
    """Relógio de mentira: o tempo só passa quando uma chamada acontece.

    Deixa o teste de orçamento medir minutos sem gastar um segundo real.
    """

    def __init__(self) -> None:
        self.agora = 0.0
        self._trava = threading.Lock()

    def monotonic(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        with self._trava:
            self.agora += segundos


@pytest.fixture
def openai_falso(monkeypatch):
    """Instala o cliente falso e devolve uma função para programar as respostas."""
    caixa: dict = {}

    def programar(respostas: list[str], liberado: bool = True, **kwargs):
        cliente = _ClienteFalso(respostas, **kwargs)
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


def _paginas_encadeadas(quantidade: int) -> list[str]:
    """Uma página por lançamento, com a cadeia 1000 → 900 → 800 → … fechando."""
    return [
        f"""
        {{"saldo_anterior": {'"1000.00"' if n == 0 else "null"},
          "transacoes": [
            {{"data": "0{n + 1}/01/2026", "historico": "PIX {n}",
             "valor": -100.00, "saldo": {1000 - 100 * (n + 1)}.00}}
          ]}}
        """
        for n in range(quantidade)
    ]



# ────────────────────────────────────────────── o relógio da leitura por imagem


def test_vision_nao_morre_no_relogio_do_caminho_de_texto(monkeypatch):
    """A regressão que apareceu no primeiro uso real: timeout no meio da leitura.

    `pdf_parse_timeout_seconds` foi dimensionado para o caminho determinístico
    (8,4s no pior extrato medido) e para a camada 2, que faz UMA chamada. A
    camada 3 faz uma por página, cada uma custando dezenas de segundos: dividindo
    o mesmo relógio, ela estourava antes da terceira página — e `pdf_max_ai_calls:
    30` prometia trinta páginas que o relógio jamais deixaria terminar.

    O relógio aqui é de mentira e só anda quando uma chamada acontece: quatro
    páginas a 40s somam 160s, muito além dos 60s do caminho de texto.
    """
    relogio = _Relogio()
    monkeypatch.setattr(pdf_parser, "time", relogio)

    ajustes = _Settings()
    ajustes.pdf_vision_concurrency = 1          # sequencial: o tempo soma
    monkeypatch.setattr(pdf_parser, "get_settings", lambda: ajustes)

    cliente = _ClienteFalso(
        _paginas_encadeadas(4), avanco_do_relogio=40.0, relogio=relogio
    )
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: cliente)

    transacoes = parse_pdf(_pdf_sem_texto(4))

    assert cliente.chamadas == 4
    assert relogio.agora > ajustes.pdf_parse_timeout_seconds
    assert len(transacoes) == 4


def test_vision_da_a_cada_chamada_o_teto_da_camada_3(openai_falso):
    """30s era o teto herdado do caminho de texto e derrubava página cheia."""
    cliente = openai_falso([_PAGINA_BOA])
    parse_pdf(_pdf_sem_texto(1))

    assert cliente.timeouts == [120.0]


def test_budget_estende_o_prazo_so_para_frente():
    orcamento = pdf_parser._PDFBudget(
        deadline=pdf_parser.time.monotonic() + 300, max_pages=120, ai_calls_restantes=30
    )
    antes = orcamento.deadline
    orcamento.estender_deadline(10)
    assert orcamento.deadline == antes


def test_vision_le_as_paginas_em_paralelo_e_devolve_na_ordem_do_arquivo(openai_falso):
    """Doze páginas em fila não cabem em teto de tempo nenhum.

    Ler em paralelo é o que torna o extrato de 12 páginas viável; a ordem que a
    cadeia de saldos confere é restaurada pelo número da página, não pela ordem
    de chegada das respostas.
    """
    cliente = openai_falso(
        _paginas_encadeadas(6), barreira=threading.Barrier(6, timeout=10)
    )
    transacoes = parse_pdf(_pdf_sem_texto(6))

    assert cliente.simultaneas_maximo == 6
    assert [t.historico for t in transacoes] == [f"PIX {n}" for n in range(6)]
    assert [t.ordem for t in transacoes] == list(range(6))


def test_vision_diz_qual_pagina_falhou_em_vez_da_recusa_generica(openai_falso):
    """Falha de chamada virava "extração não verificável" — culpa no extrato errado.

    A frase antiga mandava o contador procurar problema no arquivo quando o que
    tinha acontecido era a chamada não ter voltado.
    """
    openai_falso([_PAGINA_BOA, _PAGINA_BOA], erro_na_pagina=1)
    with pytest.raises(PDFParseError, match="falhou na página 2"):
        parse_pdf(_pdf_sem_texto(2))


# ─────────────────────────────── camada de texto que não é texto


def test_fracao_legivel_separa_glifo_sem_traducao_de_texto_de_verdade():
    """Fonte sem `ToUnicode` devolve o código do glifo, e ele CONTA como caractere.

    Os números do lado direito são os medidos nos seis extratos da JS Bertoldo
    que motivaram isto — não são inventados para o teste.
    """
    extrato = ["02/02/2026 0000 13128 500 BB GIRO PRONAMPE 300.712 1.350,95 D",
               "03/02/2026 0000 14397 821 Pix - Recebido 31.806 3.000,00 C"]
    assert pdf_parser._fracao_legivel(extrato) > 0.95

    glifos = ["(cid:0)(cid:1)(cid:2)(cid:3)(cid:4)(cid:2)(cid:5)(cid:6)",
              "(cid:26)(cid:27)(cid:28)(cid:29)(cid:30)(cid:31)"]
    assert pdf_parser._fracao_legivel(glifos) < 0.2

    # Área de uso privado: mesmo problema, outra forma (o caso do Painel do BB).
    assert pdf_parser._fracao_legivel([chr(0xE940) * 50]) < 0.2

    assert pdf_parser._fracao_legivel([]) == 0.0

    # O limiar cai no meio de um vão enorme — não é número escolhido a dedo.
    assert 0.2 < pdf_parser._FRACAO_LEGIVEL_MINIMA < 0.9


def test_pdf_com_texto_ilegivel_vai_para_a_camada_de_imagem(openai_falso, monkeypatch):
    """A regressão real: 32.960 caracteres de lixo passavam no teto de 50.

    Dois extratos do Itaú da JS Bertoldo renderizam perfeitamente e são do mesmo
    layout que o adaptador `itau` já lê — mas a fonte não traz `ToUnicode`, o
    pdfplumber devolvia `(cid:N)` e a contagem de caracteres ficava ALTA. Com o
    portão medindo tamanho em vez de legibilidade, eles entravam no caminho de
    texto, não extraíam nada e eram recusados, sem nunca chegar na Vision.
    """
    lixo = [f"(cid:{n})" for n in range(400)]
    monkeypatch.setattr(
        pdf_parser, "_extrair_linhas_pdfplumber", lambda *a, **k: (lixo, len("".join(lixo)))
    )

    cliente = openai_falso([_PAGINA_BOA])
    transacoes = parse_pdf(_pdf_sem_texto(1))

    assert cliente.chamadas_com_imagem == 1, "o arquivo tinha de chegar na camada de imagem"
    assert [t.valor for t in transacoes] == [Decimal("-100.00"), Decimal("50.00")]


def test_pdf_com_texto_legivel_nao_gasta_chamada_de_ia(openai_falso, monkeypatch):
    """O contrapeso: texto de verdade não pode ser mandado para a imagem.

    Sem esta asserção, apertar o limiar por engano transformaria todo extrato
    lido de graça numa chamada paga, e nada falharia.
    """
    linhas = ["02/02/2026 0000 13128 500 BB GIRO PRONAMPE 300.712 1.350,95 D"] * 20
    monkeypatch.setattr(
        pdf_parser, "_extrair_linhas_pdfplumber", lambda *a, **k: (linhas, len("".join(linhas)))
    )

    cliente = openai_falso([_PAGINA_BOA])
    with pytest.raises(PDFParseError):
        parse_pdf(_pdf_sem_texto(1))

    # Uma chamada de TEXTO (camada 2) é o esperado; nenhuma de imagem.
    assert cliente.chamadas_com_imagem == 0, "texto legível não pode virar chamada de imagem"


def test_resposta_cortada_no_meio_e_recusada_em_vez_de_virar_pagina_vazia(openai_falso):
    """A falha mais traiçoeira desta camada, encontrada rodando os extratos reais.

    Uma página com muitos lançamentos estoura o teto de tokens da resposta. O
    JSON fica inválido, `_parse_ai_response_vision` devolve lista vazia com um
    aviso no log, e o arquivo é recusado por "extração não verificável" —
    culpando o extrato por um teto nosso.
    """
    cliente = openai_falso([_PAGINA_BOA], truncar_na_pagina=0)
    with pytest.raises(PDFParseError, match="veio incompleta"):
        parse_pdf(_pdf_sem_texto(1))
    assert cliente.chamadas_com_imagem == 1


def test_saldo_do_dia_ancora_o_ultimo_lancamento_daquele_dia():
    """Há layouts em que o saldo só existe na linha de saldo do dia.

    O Itaú "lançamentos do período" é um: nenhum lançamento traz saldo, e sem
    aproveitar as linhas `SALDO TOTAL DISPONÍVEL DIA` o extrato não tem âncora
    nenhuma. Encaixar isso é trabalho de código — quando o modelo tentava, ele
    grudava o saldo no lançamento vizinho de cima, e como o extrato sai do mais
    recente para o mais antigo o saldo de um dia ia parar no dia seguinte.
    """
    from datetime import date as _date

    def _t(dia: int, valor: str, ordem: int):
        return pdf_parser.TransacaoOFX(
            fitid=f"f{ordem}", data=_date(2026, 5, dia), valor=Decimal(valor),
            historico=f"LANC {ordem}", tipo_ofx="DEBIT", saldo_apos=None, ordem=ordem,
        )

    transacoes = [_t(21, "-10.00", 0), _t(22, "-20.00", 1), _t(22, "-30.00", 2)]
    saldos = [(_date(2026, 5, 21), Decimal("100.00")),
              (_date(2026, 5, 22), Decimal("50.00"))]

    resultado = pdf_parser._ancorar_saldos_do_dia(transacoes, saldos)

    # Cada saldo prende no ÚLTIMO lançamento do seu dia, e só nele.
    assert [t.saldo_apos for t in resultado] == [
        Decimal("100.00"), None, Decimal("50.00")
    ]


def test_saldo_impresso_na_linha_manda_mais_que_o_saldo_do_dia():
    """Se a própria linha traz saldo, ele é do banco e não pode ser sobrescrito."""
    from datetime import date as _date

    transacao = pdf_parser.TransacaoOFX(
        fitid="f0", data=_date(2026, 5, 21), valor=Decimal("-10.00"),
        historico="LANC", tipo_ofx="DEBIT", saldo_apos=Decimal("7.00"), ordem=0,
    )
    (resultado,) = pdf_parser._ancorar_saldos_do_dia(
        [transacao], [(_date(2026, 5, 21), Decimal("999.00"))]
    )
    assert resultado.saldo_apos == Decimal("7.00")


def test_fecho_da_conta_corrente_vem_do_rotulo_e_nao_da_capa():
    """O extrato do Itaú declara TRÊS saldos, e só um fecha a conta corrente.

        capa    saldo em 25/02/26   5.969,14-  ┐ conta corrente MAIS a
        capa    saldo em 31/03/26  17.138,32   ┘ aplicação automática
        rodapé  Saldo em C/C            1,00     só a conta corrente

    Os 17.138,32 são 1,00 de conta corrente com 17.137,32 aplicados. Fechar a
    cadeia com eles acusa uma diferença de 17.137,32 que não é lançamento
    nenhum — é o dinheiro que está na aplicação. Foi exatamente onde este
    extrato parou antes de o rótulo passar a decidir.
    """
    from datetime import date as _date

    transacoes = [
        pdf_parser.TransacaoOFX(
            fitid="f0", data=_date(2026, 3, 2), valor=Decimal("-10.00"),
            historico="X", tipo_ofx="DEBIT", saldo_apos=None, ordem=0,
        ),
        pdf_parser.TransacaoOFX(
            fitid="f1", data=_date(2026, 3, 31), valor=Decimal("-20.00"),
            historico="Y", tipo_ofx="DEBIT", saldo_apos=None, ordem=1,
        ),
    ]
    declarados = [
        ("saldo em 25/02/26", _date(2026, 2, 25), Decimal("-5969.14")),
        ("saldo em 31/03/26", _date(2026, 3, 31), Decimal("17138.32")),
        ("Saldo em C/C", None, Decimal("1.00")),
    ]

    abertura, fecho = pdf_parser._pontas_declaradas(declarados, transacoes)
    assert abertura == Decimal("-5969.14")
    assert fecho == Decimal("1.00"), "a capa soma a aplicação e não fecha a conta"


def test_sem_rotulo_de_conta_corrente_nao_ha_fecho():
    """Melhor ficar sem cauda coberta do que fechar com o número errado."""
    from datetime import date as _date

    transacoes = [pdf_parser.TransacaoOFX(
        fitid="f0", data=_date(2026, 3, 2), valor=Decimal("-10.00"),
        historico="X", tipo_ofx="DEBIT", saldo_apos=None, ordem=0,
    )]
    _, fecho = pdf_parser._pontas_declaradas(
        [("saldo em 31/03/26", _date(2026, 3, 31), Decimal("17138.32"))], transacoes
    )
    assert fecho is None


# ─────────────────────── documento que não é extrato de conta


def test_extrato_de_rendimentos_e_recusado_dizendo_o_que_e():
    """Recusar com "extração não verificável" manda procurar defeito onde não há.

    O escritório subiu, junto dos extratos, dois "Extrato de Rendimentos —
    Caixinhas PJ" da Nu Financeira: relatório de aplicação financeira, sem
    lançamento bancário nenhum. O arquivo está perfeito; ele só não é do tipo
    que este módulo lê, e a mensagem precisa dizer isso.
    """
    from src.domain.extrato.pdf_parser import _documento_de_outro_tipo

    rendimentos = [
        "Extrato de Rendimentos",
        "Período: 01 JAN 2026 a 31 JAN 2026",
        "Caixinhas PJ",
        "Data Movimentação Rendimento Valor bruto IR IOF Saldo Líquido",
    ]
    recusa = _documento_de_outro_tipo(rendimentos)
    assert recusa is not None
    assert "RENDIMENTOS" in recusa
    assert "conta bancária" in recusa


def test_extrato_de_conta_que_fala_de_rendimento_nao_e_confundido():
    """O contrapeso — e ele é necessário, não decorativo.

    Extrato de conta corrente fala de rendimento o tempo todo: o Itaú imprime
    `Rend Pago Aplic Aut Mais` em quase todo dia. Marcador ancorado em palavra
    solta trocaria uma recusa confusa por uma recusa ERRADA, que é pior.
    """
    from src.domain.extrato.pdf_parser import _documento_de_outro_tipo

    extrato_itau = [
        "extrato mensal ag 3706 cc 40100-1 mar 2026",
        "data descrição entradas R$ saídas R$ saldo R$",
        "25/02 Saldo anterior 5.969,14-",
        "16/03 Rend Pago Aplic Aut Mais 0,01",
        "16/03 Res Aplic Aut Mais 2.890,95",
    ]
    assert _documento_de_outro_tipo(extrato_itau) is None
