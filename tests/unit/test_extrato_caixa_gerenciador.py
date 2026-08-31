"""Caixa — layout "Gerenciador CAIXA", o mais espinhoso dos seis da JS BERTOLDO.

Coordenadas medidas no arquivo real `CAIXA MES 02 26.pdf`. Três coisas o
separam do layout que o adaptador já lia:

  - o sinal é um `-` SOLTO na coluna de valor, não a letra `D`/`C` à direita;
  - células quebram em duas alturas — o histórico e até um valor;
  - **o saldo não traz sinal nenhum**, e a conta está no vermelho.

O terceiro é o que exige cuidado. Importar como impresso poria +R$ 84.951,75 no
razão de uma conta que DEVE esse tanto.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.domain.extrato.bancos import caixa
from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

TOLERANCIA = Decimal("0.05")


def _p(texto: str, x0: float, x1: float, top: float) -> dict:
    return {"text": texto, "x0": x0, "x1": x1, "top": top}


_CABECALHO = [
    _p("Data", 28, 51, 175.5), _p("de", 54, 65, 175.5),
    _p("Data", 134, 157, 175.5), _p("de", 160, 171, 175.5),
    _p("Documento", 238, 289, 182.2), _p("Histórico", 304, 342, 182.2),
    _p("Valor(R$)", 447, 488, 182.2), _p("Saldo(R$)", 511, 553, 182.2),
    _p("lançamento", 28, 81, 189.0), _p("movimento", 134, 184, 189.0),
]

_GERENCIADOR = [
    *_CABECALHO,

    _p("02/02/2026", 28, 80, 219.4), _p("02/02/2026", 134, 186, 219.4),
    _p("0", 238, 243, 219.4),
    _p("DEBITO", 304, 335, 219.4), _p("DE", 337, 348, 219.4),
    _p("IOF", 351, 366, 219.4),
    _p("-", 460, 464, 219.4), _p("197,96", 466, 496, 219.4),
    _p("R$", 511, 522, 219.4), _p("63.725,71", 524, 567, 219.4),

    _p("02/02/2026", 28, 80, 247.2), _p("02/02/2026", 134, 186, 247.2),
    _p("0", 238, 243, 247.2),
    _p("COBRANCA", 304, 355, 247.2), _p("DE", 358, 369, 247.2),
    _p("JUROS", 371, 399, 247.2),
    _p("-", 452, 456, 247.2), _p("9.710,68", 459, 496, 247.2),
    _p("R$", 511, 522, 247.2), _p("73.436,39", 524, 567, 247.2),

    # Fecha o dia e não é lançamento: valor 0,00 e saldo repetido.
    _p("02/02/2026", 28, 80, 274.2), _p("02/02/2026", 134, 186, 274.2),
    _p("0", 238, 243, 274.2),
    _p("SALDO", 304, 334, 274.2), _p("DIA", 336, 351, 274.2),
    _p("0,00", 478, 496, 274.2),
    _p("R$", 511, 522, 274.2), _p("73.436,39", 524, 567, 274.2),

    # Histórico quebrado em DUAS alturas, com o corpo do lançamento no meio.
    _p("MENSALIDADE", 304, 365, 301.2), _p("CESTA", 367, 393, 301.2),
    _p("05/02/2026", 28, 80, 307.2), _p("05/02/2026", 134, 186, 307.2),
    _p("202601", 238, 271, 307.2),
    _p("-", 460, 464, 307.2), _p("125,00", 466, 496, 307.2),
    _p("R$", 511, 522, 307.2), _p("73.561,39", 524, 567, 307.2),
    _p("SERVICO", 304, 341, 313.9),

    _p("02/03/2026", 28, 80, 367.9), _p("02/03/2026", 134, 186, 367.9),
    _p("0", 238, 243, 367.9),
    _p("DEBITO", 304, 335, 367.9), _p("DE", 337, 348, 367.9),
    _p("IOF", 351, 366, 367.9),
    _p("-", 460, 464, 367.9), _p("212,20", 466, 496, 367.9),
    _p("R$", 511, 522, 367.9), _p("73.773,59", 524, 567, 367.9),

    # O `-` do valor ficou numa altura ACIMA do número.
    _p("-", 493, 496, 394.9),
    _p("02/03/2026", 28, 80, 401.7), _p("02/03/2026", 134, 186, 401.7),
    _p("0", 238, 243, 401.7),
    _p("COBRANCA", 304, 355, 401.7), _p("DE", 358, 369, 401.7),
    _p("JUROS", 371, 399, 401.7),
    _p("R$", 511, 522, 401.7), _p("84.951,75", 524, 567, 401.7),
    _p("11.178,16", 453, 496, 407.7),
]


def test_gerenciador_extrai_so_os_lancamentos_de_verdade():
    """`SALDO DIA` fecha o dia com valor 0,00 — não é movimento."""
    (bloco,) = caixa.extrair_de_palavras([_GERENCIADOR], 2026)
    assert [t.historico for t in bloco.transacoes] == [
        "DEBITO DE IOF", "COBRANCA DE JUROS", "MENSALIDADE CESTA SERVICO",
        "DEBITO DE IOF", "COBRANCA DE JUROS",
    ]


def test_gerenciador_inverte_o_saldo_devedor_impresso_sem_sinal():
    """O ponto perigoso: a CAIXA imprime o saldo devedor em módulo.

    Um débito de 9.710,68 faz o número CRESCER de 63.725,71 para 73.436,39 —
    impossível num saldo credor. Importar como impresso poria a conta no azul.

    A inversão não é heurística de descrição: as duas leituras são testadas
    contra a cadeia e vale a que caminha.
    """
    (bloco,) = caixa.extrair_de_palavras([_GERENCIADOR], 2026)
    assert [t.saldo_apos for t in bloco.transacoes] == [
        Decimal("-63725.71"), Decimal("-73436.39"), Decimal("-73561.39"),
        Decimal("-73773.59"), Decimal("-84951.75"),
    ]
    assert _validar_blocos([bloco], TOLERANCIA) is True


def test_gerenciador_le_o_menos_solto_como_sinal_do_valor():
    (bloco,) = caixa.extrair_de_palavras([_GERENCIADOR], 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-197.96"), Decimal("-9710.68"), Decimal("-125.00"),
        Decimal("-212.20"), Decimal("-11178.16"),
    ]


def test_gerenciador_junta_a_celula_de_valor_quebrada_em_duas_alturas():
    """O `-` numa altura e o `11.178,16` na de baixo são um valor só.

    Sem juntar, o lançamento de R$ 11.178,16 sairia POSITIVO — e a cadeia,
    que é quem cobra, acusaria o dobro do valor de diferença.
    """
    (bloco,) = caixa.extrair_de_palavras([_GERENCIADOR], 2026)
    assert bloco.transacoes[-1].valor == Decimal("-11178.16")


def test_gerenciador_ordena_o_historico_quebrado_pela_altura():
    """`MENSALIDADE CESTA` em cima, `SERVICO` embaixo.

    Ordenar pelo x0 embaralha as metades e sai "MENSALIDADE SERVICO CESTA".
    """
    (bloco,) = caixa.extrair_de_palavras([_GERENCIADOR], 2026)
    assert bloco.transacoes[2].historico == "MENSALIDADE CESTA SERVICO"


def test_gerenciador_nao_inverte_quando_nenhuma_leitura_fecha():
    """No empate não se escolhe sinal: deixa como veio e a conferência recusa.

    Aqui um saldo foi adulterado, então nem os saldos impressos nem os
    invertidos caminham. Inverter mesmo assim seria trocar um erro visível por
    um invisível.
    """
    adulterado = [
        p if p["text"] != "73.436,39" else {**p, "text": "99.999,99"}
        for p in _GERENCIADOR
    ]
    (bloco,) = caixa.extrair_de_palavras([adulterado], 2026)
    assert bloco.transacoes[0].saldo_apos == Decimal("63725.71")
    with pytest.raises(PDFParseError, match="não caminha"):
        _validar_blocos([bloco], TOLERANCIA)


def test_gerenciador_e_reconhecido_pelo_conteudo():
    """Sem isto, só chega ao adaptador quem tem `CAIXA` na agência."""
    assert caixa.reconhece(
        ["Documento Histórico Valor(R$) Saldo(R$)", "lançamento movimento"]
    ) is True


# ── terceiro layout: "Extrato por período", com D/C explícito ────────────────
#
#     Data Mov.   Nr. Doc.  Histórico        Valor        Saldo
#                 000000    SALDO ANTERIOR    0,00   57.315,62 D
#     02/01/2026  000000    DEB IOF         215,80 D 57.531,42 D
#     02/01/2026  000000    SALDO DIA         0,00 C 65.315,41 D
#
# É o mais simples dos três: a letra diz o sinal do valor E do saldo, então não
# há nada a inferir. Só o cabeçalho muda — o corpo que lê o layout com D/C lê
# este igual, e é por isso que ele não ganhou extração própria.

_PERIODO = [
    _p("Data", 40, 58, 225.1), _p("Mov.", 61, 78, 225.1),
    _p("Nr.", 102, 112, 225.1), _p("Doc.", 115, 132, 225.1),
    _p("Histórico", 168, 201, 225.1),
    _p("Valor", 399, 418, 225.1), _p("Saldo", 525, 546, 225.1),

    _p("02/01/2026", 40, 85, 265.6), _p("000000", 102, 130, 265.6),
    _p("DEB", 168, 184, 265.6), _p("IOF", 187, 200, 265.6),
    _p("215,80", 383, 410, 265.6), _p("D", 412, 418, 265.6),
    _p("57.531,42", 499, 538, 265.6), _p("D", 540, 546, 265.6),

    _p("02/01/2026", 40, 85, 285.8), _p("000000", 102, 130, 285.8),
    _p("DEB", 168, 184, 285.8), _p("JUROS", 187, 212, 285.8),
    _p("7.783,99", 376, 410, 285.8), _p("D", 412, 418, 285.8),
    _p("65.315,41", 499, 538, 285.8), _p("D", 540, 546, 285.8),

    _p("02/01/2026", 40, 85, 306.1), _p("000000", 102, 130, 306.1),
    _p("SALDO", 168, 194, 306.1), _p("DIA", 197, 211, 306.1),
    _p("0,00", 393, 410, 306.1), _p("C", 413, 418, 306.1),
    _p("65.315,41", 499, 538, 306.1), _p("D", 540, 546, 306.1),

    _p("08/01/2026", 40, 85, 326.3), _p("000033", 102, 130, 326.3),
    _p("CRED", 168, 190, 326.3), _p("TED", 193, 210, 326.3),
    _p("4.932,00", 376, 410, 326.3), _p("C", 413, 418, 326.3),
    _p("60.383,41", 499, 538, 326.3), _p("D", 540, 546, 326.3),
]


def test_periodo_le_o_sinal_da_letra_no_valor_e_no_saldo():
    """Nada a inferir aqui: a letra diz tudo, inclusive que o saldo é devedor."""
    (bloco,) = caixa.extrair_de_palavras([_PERIODO], 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-215.80"), Decimal("-7783.99"), Decimal("4932.00")
    ]
    assert [t.saldo_apos for t in bloco.transacoes] == [
        Decimal("-57531.42"), Decimal("-65315.41"), Decimal("-60383.41")
    ]


def test_periodo_ignora_saldo_dia():
    (bloco,) = caixa.extrair_de_palavras([_PERIODO], 2026)
    assert all("SALDO DIA" not in t.historico for t in bloco.transacoes)


def test_periodo_e_reconhecido_pelo_conteudo():
    assert caixa.reconhece(
        ["Data Mov. Nr. Doc. Histórico Valor Saldo",
         "02/01/2026 000000 DEB IOF 215,80 D 57.531,42 D"]
    ) is True
