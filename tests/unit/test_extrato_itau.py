"""Adaptador do Itaú — os dois layouts, lidos por coordenada.

As palavras abaixo reproduzem a geometria real medida nos extratos (posições de
coluna, alinhamento à direita dos valores, altura das linhas), com razão social,
CNPJs e valores fictícios. Nenhum dado de cliente entra no repositório.

Por que este adaptador não trabalha por linha de texto: a página do extrato
mensal tem duas colunas, e o `extract_text` do pdfplumber intercala a legenda
lateral no meio dos lançamentos. "pelaBolsa de Valores 31/12 Saldo anterior
19.477,03-" sai como uma linha só — não há regex que desfaça isso depois.

Os três defeitos que estes testes travam, todos encontrados medindo contra
extratos reais:

1. A linha `na conta corrente (1)`, do "totalizador de aplicações automáticas"
   impresso DEPOIS do fecho, entrava como um crédito de R$ 19.070,30 que não
   existe — e entrava na cauda, onde nenhuma âncora de saldo a pegaria.
2. O `Saldo final` soma a aplicação automática; usá-lo para fechar a cadeia da
   conta corrente acusava uma diferença que era só o saldo aplicado.
3. No extrato do internet banking, o cabeçalho da página (razão social e CNPJ do
   TITULAR) era o texto solto mais próximo do primeiro lançamento e virava a
   contraparte dele.
"""

from decimal import Decimal

import pytest

from src.domain.extrato.bancos import itau
from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

TOLERANCIA = Decimal("0.05")


def _p(texto: str, x0: float, top: float, largura: float = 0.0) -> dict:
    """Uma palavra com geometria. `largura` cai no padrão de ~3,5pt por caractere."""
    return {
        "text": texto,
        "x0": x0,
        "x1": x0 + (largura or len(texto) * 3.5),
        "top": top,
    }


def _direita(texto: str, x1: float, top: float) -> dict:
    """Palavra alinhada pela borda DIREITA — como as colunas numéricas."""
    largura = len(texto) * 3.5
    return {"text": texto, "x0": x1 - largura, "x1": x1, "top": top}


# ───────────────────────────────────────────────── extrato mensal (duas colunas)

# Bordas medidas no extrato real: data x0=150, descrição x0=208,
# entradas x1=396, saídas x1=456, saldo x1=549.
_CABECALHO_MENSAL = [
    _p("data", 150, 588),
    _p("descrição", 208, 588),
    _p("entradas", 357, 588),
    _p("R$", 388, 588, largura=8),
    _p("saídas", 426, 588),
    _p("R$", 448, 588, largura=8),
    _p("saldo", 521, 588),
    _p("R$", 541, 588, largura=8),
]

# A legenda lateral, que o extract_text intercala nos lançamentos.
_LEGENDA = [
    _p("A", 56, 586),
    _p("=agendamento", 62, 586),
    _p("pelaBolsa", 69, 606),
    _p("de", 101, 606),
    _p("Valores", 110, 606),
    _p("C", 56, 616),
    _p("=", 62, 616),
    _p("crédito", 68, 616),
]

_MENSAL = [
    *_CABECALHO_MENSAL,
    *_LEGENDA,
    # 31/12 Saldo anterior 1.000,00-
    _p("31/12", 150, 606),
    _p("Saldo", 208, 606),
    _p("anterior", 225, 606),
    _direita("1.000,00-", 552, 606),
    # 02/01 Fin Veíc — débito, sem saldo na linha
    _p("02/01", 150, 630),
    _p("Fin", 208, 630),
    _p("Veíc", 218, 630),
    _direita("400,00-", 458, 630),
    # JUROS LIMITE — débito, com saldo
    _p("JUROS", 208, 640),
    _p("LIMITE", 228, 640),
    _direita("100,00-", 458, 640),
    _direita("1.500,00-", 552, 640),
    # Sispag — crédito, sem saldo
    _p("Sispag", 208, 683),
    _p("EXEMPLO", 230, 683),
    _direita("2.000,00", 395, 683),
    # SALDO APLIC AUT MAIS — saldo de aplicação, não é lançamento nem âncora
    _p("SALDO", 208, 751),
    _p("APLIC", 230, 751),
    _p("AUT", 248, 751),
    _p("MAIS", 262, 751),
    _direita("385,73", 549, 751),
    # Saldo em C/C — fecha a cadeia da conta corrente
    _p("Saldo", 208, 762),
    _p("em", 227, 762),
    _p("C/C", 238, 762),
    _direita("500,00", 549, 762),
    # Saldo final — encerra a tabela (soma a aplicação, não ancora)
    _p("Saldo", 208, 772),
    _p("final", 229, 772),
    _direita("885,73", 549, 772),
    # Depois do fecho: o totalizador de aplicações, que NÃO é lançamento
    _p("na", 210, 800),
    _p("conta", 219, 800),
    _p("corrente", 239, 800),
    _p("(1)", 268, 800),
    _direita("19.070,30", 395, 800),
    _direita("22.393,16-", 458, 800),
]


def test_mensal_descarta_a_legenda_lateral():
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    assert all("pelaBolsa" not in t.historico for t in bloco.transacoes)
    assert all("agendamento" not in t.historico for t in bloco.transacoes)


def test_mensal_tira_o_sinal_da_coluna_e_nao_do_numero():
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-400.00"),
        Decimal("-100.00"),
        Decimal("2000.00"),
    ]


def test_mensal_le_o_saldo_anterior_da_abertura():
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    assert bloco.saldo_anterior == Decimal("-1000.00")


def test_mensal_ignora_saldo_de_aplicacao_como_lancamento_e_como_ancora():
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    assert all("APLIC" not in t.historico for t in bloco.transacoes)
    # 385,73 é saldo da aplicação: não pode ter virado saldo de nenhuma linha.
    assert Decimal("385.73") not in [t.saldo_apos for t in bloco.transacoes]


def test_mensal_fecha_a_cadeia_no_saldo_em_cc_e_nao_no_saldo_final():
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    assert bloco.saldo_final == Decimal("500.00")


def test_mensal_para_no_fecho_e_nao_le_o_totalizador_de_aplicacoes():
    """A linha `na conta corrente (1)` vem depois do fecho e não é lançamento."""
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    assert len(bloco.transacoes) == 3
    assert all("conta corrente" not in t.historico for t in bloco.transacoes)
    assert Decimal("19070.30") not in [t.valor for t in bloco.transacoes]


def test_mensal_a_cadeia_fecha_de_ponta_a_ponta():
    blocos = itau.extrair_de_palavras([_MENSAL], 2026)
    assert _validar_blocos(blocos, TOLERANCIA) is True


def test_mensal_soma_reconcilia_abertura_e_fecho():
    (bloco,) = itau.extrair_de_palavras([_MENSAL], 2026)
    soma = sum((t.valor for t in bloco.transacoes), Decimal("0"))
    assert bloco.saldo_anterior + soma == bloco.saldo_final


# ──────────────────────────────────────── extrato do internet banking (1 coluna)

# Bordas medidas: data x0=35, lançamentos x0=91, razão social x0=227,
# CNPJ x0=364, valor x1=509, saldo x1=558.
_INTERNET = [
    # Cabeçalho da PÁGINA — razão social e CNPJ do titular, acima da tabela.
    _p("EXEMPLO", 35, 150),
    _p("TRANSPORTES", 91, 150),
    _p("LTDA", 160, 150),
    _p("CNPJ", 200, 150),
    _p("11.111.111/0001-11", 230, 150),
    # Cabeçalho da TABELA
    _p("Data", 35, 207),
    _p("Lançamentos", 91, 207),
    _p("Razão", 227, 207),
    _p("Social", 252, 207),
    _p("CNPJ/CPF", 364, 207),
    _p("Valor", 472, 207),
    _p("(R$)", 494, 207, largura=15),
    _p("Saldo", 521, 207),
    _p("(R$)", 543, 207, largura=15),
    # Saldo anterior
    _p("29/06/2026", 35, 220, largura=43),
    _p("SALDO", 91, 220),
    _p("ANTERIOR", 119, 220),
    _direita("1.000,00", 558, 220),
    # Razão social quebrada ACIMA da linha de dados
    _p("EXEMPLO", 227, 232),
    _p("LOGISTICA", 257, 232),
    # Linha de dados
    _p("30/06/2026", 35, 238, largura=43),
    _p("PIX", 91, 238),
    _p("ENVIADO", 105, 238),
    _p("22.222.222/0001-22", 364, 238),
    _direita("-200,00", 509, 238),
    # Razão social quebrada ABAIXO da linha de dados
    _p("TRANSPORTES", 227, 243),
    _p("LTDA", 285, 243),
    # Segundo lançamento
    _p("30/06/2026", 35, 256, largura=43),
    _p("DEB", 91, 256),
    _p("AUTOR", 108, 256),
    _direita("-300,00", 509, 256),
    # Fecho do dia — âncora
    _p("30/06/2026", 35, 270, largura=43),
    _p("SALDO", 91, 270),
    _p("TOTAL", 119, 270),
    _p("DISPONÍVEL", 147, 270),
    _p("DIA", 194, 270),
    _direita("500,00", 558, 270),
]


def test_internet_le_data_completa_e_valor_com_sinal_no_numero():
    (bloco,) = itau.extrair_de_palavras([_INTERNET], 2026)
    assert [t.data.isoformat() for t in bloco.transacoes] == [
        "2026-06-30",
        "2026-06-30",
    ]
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-200.00"),
        Decimal("-300.00"),
    ]


def test_internet_junta_a_razao_social_quebrada_acima_e_abaixo():
    (bloco,) = itau.extrair_de_palavras([_INTERNET], 2026)
    historico = bloco.transacoes[0].historico
    assert "EXEMPLO LOGISTICA" in historico
    assert "TRANSPORTES LTDA" in historico
    assert "22.222.222/0001-22" in historico


def test_internet_nao_cola_o_cabecalho_da_pagina_no_primeiro_lancamento():
    """O CNPJ do titular não pode virar a contraparte do primeiro lançamento."""
    (bloco,) = itau.extrair_de_palavras([_INTERNET], 2026)
    assert "11.111.111/0001-11" not in bloco.transacoes[0].historico


def test_internet_usa_o_fecho_do_dia_como_ancora_da_cadeia():
    (bloco,) = itau.extrair_de_palavras([_INTERNET], 2026)
    assert bloco.saldo_anterior == Decimal("1000.00")
    # A âncora do dia é copiada para o último lançamento anterior a ela.
    assert bloco.transacoes[-1].saldo_apos == Decimal("500.00")
    assert bloco.transacoes[0].saldo_apos is None


def test_internet_a_cadeia_do_dia_fecha():
    blocos = itau.extrair_de_palavras([_INTERNET], 2026)
    assert _validar_blocos(blocos, TOLERANCIA) is True


def test_internet_lancamento_perdido_no_meio_do_dia_quebra_a_cadeia():
    """O segmento é o dia inteiro: some uma linha e a soma deixa de fechar."""
    sem_o_segundo = [
        palavra
        for palavra in _INTERNET
        if not (palavra["top"] == 256 or palavra["text"] == "-300,00")
    ]
    blocos = itau.extrair_de_palavras([sem_o_segundo], 2026)
    with pytest.raises(PDFParseError, match="não caminha"):
        _validar_blocos(blocos, TOLERANCIA)


def test_variantes_sao_escolhidas_pelo_cabecalho_da_tabela():
    (mensal,) = itau.extrair_de_palavras([_MENSAL], 2026)
    (internet,) = itau.extrair_de_palavras([_INTERNET], 2026)
    assert len(mensal.transacoes) == 3
    assert len(internet.transacoes) == 2
    assert itau.extrair_de_palavras([[_p("nada", 10, 10)]], 2026) == []


# ─────────────────────────────────────────────────────── detecção sem falso positivo

# Cabeçalhos reais de OUTROS bancos que já foram reconhecidos como Itaú por
# engano. Os três casos vieram de rodar a detecção contra as 20 amostras:
# o Nubank casava com a busca pela marca "itau" no texto; Stone e Grafeno
# casavam com um cabeçalho genérico de "data lançamento valor saldo".
_CABECALHOS_DE_OUTROS_BANCOS = [
    "DATA TIPO LANÇAMENTO VALOR (R$) SALDO (R$) CONTRAPARTE",
    "DATA / HORA LANÇAMENTO NOME · DOC · BANCO / AG / CONTA VALOR (R$) SALDO (R$)",
    "Data Descrição Documento Valor (R$) Saldo (R$)",
    "Pix enviado para conta do Itau em 02/01/2026",
]


@pytest.mark.parametrize("linha", _CABECALHOS_DE_OUTROS_BANCOS)
def test_nao_reconhece_extrato_de_outro_banco(linha):
    assert itau.reconhece([linha]) is False


def test_reconhece_as_duas_variantes_pelo_cabecalho():
    assert itau.reconhece(["data descrição entradas R$ saídas R$ saldo R$"]) is True
    assert itau.reconhece(
        ["Data Lançamentos Razão Social CNPJ/CPF Valor (R$) Saldo (R$)"]
    ) is True
