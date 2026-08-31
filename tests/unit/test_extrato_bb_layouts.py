"""Banco do Brasil — os dois layouts que a JS BERTOLDO não conseguia subir.

As coordenadas aqui não são inventadas: foram medidas nos arquivos reais
`Banco do Brasil JSBER MES02 2026.pdf` e
`Painel - Extrato - Conta Corrente jsber mes 01 2026.pdf`. É por isso que os
números parecem arbitrários — eles são o que o pdfplumber devolve.

Os dois casos têm a mesma assinatura de falha, e é a pior que existe: o
adaptador era escolhido, reconhecia o banco, extraía ZERO lançamentos e o
arquivo era recusado com "não foi possível obter uma extração verificável".
Nada apontava para o cabeçalho.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.domain.extrato.bancos import bb
from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

TOLERANCIA = Decimal("0.05")


def _p(texto: str, x0: float, x1: float, top: float) -> dict:
    return {"text": texto, "x0": x0, "x1": x1, "top": top}


# ── Layout "Consultas": cabeçalho empilhado em três alturas ──────────────────
#
#     Dt.        Dt.                                              ← 199,1
#     Ag. origem  Lote  Histórico  Documento  Valor R$  Saldo     ← 203,6
#     balancete   movimento                                       ← 207,6

_CONSULTAS = [
    _p("Dt.", 65, 75, 199.1), _p("Dt.", 109, 119, 199.1),

    _p("Ag.", 139, 151, 203.6), _p("origem", 153, 176, 203.6),
    _p("Lote", 189, 203, 203.6), _p("Histórico", 212, 241, 203.6),
    _p("Documento", 404, 442, 203.6), _p("Valor", 470, 487, 203.6),
    _p("R$", 489, 499, 203.6), _p("Saldo", 531, 550, 203.6),

    _p("balancete", 54, 87, 207.6), _p("movimento", 95, 132, 207.6),

    # Abertura: o valor sai na coluna de SALDO, e o `000` na frente é o código
    # do histórico — era ele que fazia "SALDO ANTERIOR" não ser reconhecido.
    _p("30/01/2026", 51, 89, 219.1), _p("0000", 149, 166, 219.1),
    _p("00000", 185, 206, 219.1), _p("000", 215, 228, 219.1),
    _p("Saldo", 230, 249, 219.1), _p("Anterior", 251, 277, 219.1),
    _p("1.031,50", 511, 540, 219.1), _p("D", 542, 547, 219.1),

    _p("02/02/2026", 51, 89, 234.6), _p("0000", 149, 166, 234.6),
    _p("13128", 185, 206, 234.6), _p("500", 215, 228, 234.6),
    _p("BB", 230, 239, 234.6), _p("GIRO", 241, 259, 234.6),
    _p("PRONAMPE", 261, 302, 234.6),
    _p("1.350,95", 459, 488, 234.6), _p("D", 490, 496, 234.6),

    _p("02/02/2026", 51, 89, 250.6), _p("0000", 149, 166, 250.6),
    _p("13128", 185, 206, 250.6), _p("807", 215, 228, 250.6),
    _p("Estorno", 230, 256, 250.6), _p("de", 258, 267, 250.6),
    _p("Débito", 270, 293, 250.6),
    _p("1.350,95", 459, 488, 250.6), _p("C", 490, 496, 250.6),

    # Fecha a cadeia: −1.031,50 −1.350,95 +1.350,95 −5,09 = −1.036,59
    _p("02/02/2026", 51, 89, 266.6), _p("0000", 149, 166, 266.6),
    _p("13601", 185, 206, 266.6), _p("118", 215, 228, 266.6),
    _p("Cobrança", 230, 266, 266.6), _p("de", 268, 277, 266.6),
    _p("I.O.F.", 279, 300, 266.6),
    _p("5,09", 469, 488, 266.6), _p("D", 490, 496, 266.6),
    _p("1.036,59", 511, 540, 266.6), _p("D", 542, 547, 266.6),
]


def test_consultas_le_o_cabecalho_empilhado_em_tres_alturas():
    """`balancete` numa altura e `Valor`/`Saldo` em outra, a 4 pontos.

    Exigir os quatro na MESMA linha fazia o layout extrair zero.
    """
    blocos = bb.extrair_de_palavras([_CONSULTAS], 2026)
    assert blocos, "o cabeçalho empilhado tem de ser reconhecido"
    (bloco,) = blocos
    assert len(bloco.transacoes) == 3


def test_consultas_descarta_o_codigo_do_historico():
    """O `500` da coluna de lote cai dentro da coluna de descrição.

    Além de sujar o histórico, ele quebrava a abertura: a descrição saía
    "000 Saldo Anterior" e o teste de início de texto não casava.
    """
    (bloco,) = bb.extrair_de_palavras([_CONSULTAS], 2026)
    assert bloco.transacoes[0].historico.startswith("BB GIRO PRONAMPE")
    assert bloco.saldo_anterior == Decimal("-1031.50")


def test_consultas_le_sinal_pela_letra_e_fecha_a_cadeia():
    (bloco,) = bb.extrair_de_palavras([_CONSULTAS], 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-1350.95"), Decimal("1350.95"), Decimal("-5.09")
    ]
    assert bloco.transacoes[-1].saldo_apos == Decimal("-1036.59")
    assert _validar_blocos([bloco], TOLERANCIA) is True


def test_consultas_e_reconhecido_pelo_conteudo():
    """Sem isto, só chega ao adaptador quem tem a sigla certa na agência."""
    linhas = [
        "Ag. origem Lote Histórico Documento Valor R$ Saldo",
        "balancete movimento",
        "30/01/2026 0000 00000 000 Saldo Anterior 1.031,50 D",
    ]
    assert bb.reconhece(linhas) is True


def test_balancete_movimento_sozinho_nao_basta_para_reconhecer():
    """A linha dos dois rótulos, sozinha, poderia ser de qualquer documento."""
    assert bb.reconhece(["balancete movimento"]) is False


# ── Layout "Painel PJ": sinal em `-R$` à esquerda do número ──────────────────

_PAINEL = [
    _p("MOVIMENTO", 183, 242, 385.8), _p("BALANCETE", 269, 323, 385.8),
    _p("HISTÓRICO", 349, 398, 385.8), _p("No.DOCUMENTO", 501, 578, 385.8),
    _p("VALOR", 663, 693, 385.8), _p("SALDO", 746, 778, 385.8),

    _p("31/12/2025", 183, 236, 417.9), _p("Saldo", 349, 374, 417.9),
    _p("Anterior", 376, 414, 417.9),
    _p("-R$", 727, 744, 417.9), _p("245,30", 746, 778, 417.9),

    _p("02/01/2026", 183, 238, 447.6), _p("Cobrança", 349, 391, 447.6),
    _p("de", 393, 405, 447.6), _p("I.O.F.", 407, 429, 447.6),
    _p("391100702", 501, 551, 447.6),
    _p("-R$", 655, 671, 447.6), _p("4,87", 674, 693, 447.6),
    _p("-R$", 730, 747, 447.6), _p("250,17", 749, 778, 447.6),

    _p("07/01/2026", 183, 236, 477.2), _p("Pix", 349, 363, 477.2),
    _p("-", 365, 370, 477.2), _p("Recebido", 372, 414, 477.2),
    _p("71514122485021", 501, 577, 477.2),
    _p("R$", 639, 651, 477.2), _p("3.000,00", 653, 693, 477.2),

    _p("07/01/2026", 183, 236, 506.8), _p("BB", 349, 361, 506.8),
    _p("GIRO", 363, 386, 506.8), _p("PRONAMPE", 388, 441, 506.8),
    _p("300711420000907", 501, 588, 506.8),
    _p("-R$", 635, 651, 506.8), _p("2.683,50", 654, 693, 506.8),
    _p("R$", 738, 750, 506.8), _p("66,33", 752, 778, 506.8),
]


def test_painel_le_o_sinal_colado_no_cifrao_antes_do_numero():
    """Nos outros dois layouts o sinal é uma letra DEPOIS do número."""
    (bloco,) = bb.extrair_de_palavras([_PAINEL], 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-4.87"), Decimal("3000.00"), Decimal("-2683.50")
    ]
    assert bloco.saldo_anterior == Decimal("-245.30")


def test_painel_ancora_a_data_em_movimento_e_nao_em_balancete():
    """A ordem do despacho é o teste de verdade aqui.

    O cabeçalho do Painel satisfaz as condições do balancete, que ancorava a
    data em `BALANCETE` (x=269) quando ela mora sob `MOVIMENTO` (x=183). Se o
    balancete voltar a ser testado primeiro, nenhuma data casa e isto zera.
    """
    (bloco,) = bb.extrair_de_palavras([_PAINEL], 2026)
    assert [t.data.isoformat() for t in bloco.transacoes] == [
        "2026-01-02", "2026-01-07", "2026-01-07"
    ]


def test_painel_traz_o_saldo_so_na_ultima_linha_do_dia_e_a_cadeia_fecha():
    (bloco,) = bb.extrair_de_palavras([_PAINEL], 2026)
    assert [t.saldo_apos for t in bloco.transacoes] == [
        Decimal("-250.17"), None, Decimal("66.33")
    ]
    assert _validar_blocos([bloco], TOLERANCIA) is True


def test_painel_lancamento_faltando_quebra_a_cadeia():
    """A rede de proteção continua valendo neste layout.

    Ler um layout novo só é ganho se a conferência continuar cobrando. Tirado o
    Pix de entrada, o saldo deixa de caminhar e a recusa diz entre quais
    lançamentos está o buraco.
    """
    sem_o_pix = [p for p in _PAINEL if p["top"] != 477.2]
    (bloco,) = bb.extrair_de_palavras([sem_o_pix], 2026)
    with pytest.raises(PDFParseError, match="não caminha"):
        _validar_blocos([bloco], TOLERANCIA)


def test_painel_e_reconhecido_pelo_conteudo():
    linhas = [
        "Dados da conta Banco: 001 - Agência: 1622 - Conta: 19530",
        "MOVIMENTO BALANCETE HISTÓRICO No.DOCUMENTO VALOR SALDO",
        "31/12/2025 Saldo Anterior -R$ 245,30",
    ]
    assert bb.reconhece(linhas) is True


# ── a linha de fecho, com as letras separadas ────────────────────────────────

_COM_FECHO = [
    *_CONSULTAS[:12],
    _p("30/03/2026", 51, 89, 219.1), _p("0000", 149, 166, 219.1),
    _p("00000", 185, 206, 219.1), _p("000", 215, 228, 219.1),
    _p("Saldo", 230, 249, 219.1), _p("Anterior", 251, 277, 219.1),
    _p("7,54", 521, 540, 219.1), _p("C", 542, 547, 219.1),

    # Lançamento SEM saldo: fica na cauda, depois da última âncora.
    _p("30/04/2026", 51, 89, 234.6), _p("0000", 149, 166, 234.6),
    _p("13601", 185, 206, 234.6), _p("123", 215, 228, 234.6),
    _p("Cobrança", 230, 266, 234.6), _p("de", 268, 277, 234.6),
    _p("Juros", 279, 300, 234.6),
    _p("6,74", 469, 488, 234.6), _p("D", 490, 496, 234.6),

    # O fecho: `S A L D O`, letra por letra, com código de histórico 999.
    _p("30/04/2026", 51, 89, 250.6), _p("0000", 149, 166, 250.6),
    _p("00000", 185, 206, 250.6), _p("999", 215, 228, 250.6),
    _p("S", 230, 235, 250.6), _p("A", 237, 242, 250.6),
    _p("L", 244, 248, 250.6), _p("D", 250, 256, 250.6),
    _p("O", 258, 264, 250.6),
    _p("0,80", 521, 540, 250.6), _p("C", 542, 547, 250.6),
]


def test_fecho_com_letras_separadas_vira_saldo_final():
    """`S A L D O` é a linha de fecho, e sem ela a cauda fica inconferível.

    O extrato de abr/2026 da JS BERTOLDO termina com um lançamento sem saldo na
    própria linha. O saldo que o cobre está na linha seguinte, com as letras
    soltas — e, não reconhecida, ela virava complemento do lançamento de cima.
    Um mês inteiro era recusado por causa da linha que justamente o fechava.
    """
    (bloco,) = bb.extrair_de_palavras([_COM_FECHO], 2026)
    assert bloco.saldo_final == Decimal("0.80")
    assert len(bloco.transacoes) == 1
    assert bloco.transacoes[0].historico.startswith("Cobrança de Juros")
    assert _validar_blocos([bloco], TOLERANCIA) is True


def test_letra_solta_na_descricao_nao_e_marcador_de_sinal():
    """O `D` de `S A L D O` estava sendo comido como sinal.

    A letra D/C era capturada em QUALQUER posição da linha. O fecho perdia o
    próprio D, virava "S A L O" e deixava de ser reconhecido.
    """
    (bloco,) = bb.extrair_de_palavras([_COM_FECHO], 2026)
    # Se o D tivesse sido consumido, o fecho não seria detectado e a linha
    # entraria como complemento do lançamento.
    assert "S A L" not in bloco.transacoes[0].historico
