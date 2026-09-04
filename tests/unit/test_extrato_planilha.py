"""Leitura de extrato em planilha.

Cada teste aqui é um defeito que a pasta de extratos do escritório produziu de
verdade, com o arquivo nomeado no docstring. As fixtures são sintéticas — o
formato é o do arquivo real, os valores não são de cliente nenhum.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest

from src.domain.extrato.planilha_parser import (
    PlanilhaParseError,
    blocos_da_planilha,
    parse_planilha,
)


def xlsx(linhas: list[list[object]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    for linha in linhas:
        wb.active.append(linha)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ──────────────────────────────────────────── o cabeçalho é quem manda

_CREDITO_DEBITO = [
    ["Extrato de Conta Corrente"],
    ["Data", "Lançamento", "Dcto.", "Crédito (R$)", "Débito (R$)", "Saldo (R$)"],
    ["30/06/2026", "SALDO ANTERIOR", None, None, None, "1.000,00"],
    ["01/07/2026", "RENTAB.INVEST", "3560100", "250,00", None, "1.250,00"],
    ["02/07/2026", "PIX ENVIADO ALFA LTDA", "2019261", None, "100,00", "1.150,00"],
]


def test_o_sinal_vem_de_qual_coluna_recebeu_o_numero():
    """O Bradesco imprime o débito SEM sinal na coluna `Débito (R$)`.

    Ler o número e confiar no que vier dentro dele transforma todo débito em
    crédito. Quem diz a direção é a coluna, não o valor — e é por isso que este
    leitor é guiado por nome de cabeçalho.
    """
    transacoes = parse_planilha(xlsx(_CREDITO_DEBITO), "extrato.xlsx")

    assert [t.valor for t in transacoes] == [Decimal("250.00"), Decimal("-100.00")]
    assert [t.tipo_ofx for t in transacoes] == ["CREDIT", "DEBIT"]


def test_saldo_anterior_abre_o_bloco_e_nao_e_lancamento():
    (bloco,) = blocos_da_planilha(xlsx(_CREDITO_DEBITO), "extrato.xlsx")

    assert bloco.saldo_anterior == Decimal("1000.00")
    assert len(bloco.transacoes) == 2
    assert all("SALDO ANTERIOR" not in t.historico for t in bloco.transacoes)


def test_a_planilha_sem_cabecalho_recusa_dizendo_o_que_faltou():
    conteudo = xlsx([["Relatório"], ["Cliente", "Observação"], ["ALFA", "nada"]])

    with pytest.raises(PlanilhaParseError, match="cabeçalho"):
        parse_planilha(conteudo, "sem_cabecalho.xlsx")


# ────────────────────────────────────── dois períodos no mesmo arquivo

def test_o_segundo_saldo_anterior_abre_um_bloco_novo():
    """`BRADESCO C&C JUL 26.XLS` emenda dois períodos: o segundo `SALDO
    ANTERIOR` está na linha 267 de 301.

    Conferir os dois como uma cadeia só acusa um salto no ponto de emenda que
    não é erro nenhum — o saldo cai de 1.150,00 para 552,00 porque começou
    outro extrato, não porque faltou lançamento.
    """
    linhas = _CREDITO_DEBITO + [
        ["04/08/2026", "SALDO ANTERIOR", None, None, None, "552,00"],
        ["10/08/2026", "PIX RECEBIDO BETA", "1145348", "48,00", None, "600,00"],
    ]

    primeiro, segundo = blocos_da_planilha(xlsx(linhas), "consolidado.xlsx")

    assert primeiro.saldo_anterior == Decimal("1000.00")
    assert segundo.saldo_anterior == Decimal("552.00")
    assert len(segundo.transacoes) == 1
    # A numeração não recomeça: `ordem` e `fitid` valem para o arquivo inteiro.
    assert [t.ordem for t in primeiro.transacoes + segundo.transacoes] == [0, 1, 2]


# ─────────────────────────────────────────────────── ordem do arquivo

_DECRESCENTE = [
    ["AGENCIA", "2190", "CONTA", "130042374"],
    ["Data", "Histórico", "Documento", "Valor (R$)", "Saldo (R$)"],
    ["30/06/2026", "PAGAMENTO DE BOLETO", "0000000000", "-100,00", "300,00"],
    ["29/06/2026", "PIX RECEBIDO ALFA", None, "150,00", "400,00"],
    ["26/06/2026", "PIX ENVIADO BETA", None, "-50,00", "250,00"],
]


def test_planilha_do_mais_recente_para_o_mais_antigo_e_invertida():
    """O Santander exporta decrescente (`Multi matriz Santander 06_26.xlsx`).

    Lida na ordem do arquivo a cadeia anda para trás em todos os lançamentos e
    o extrato inteiro é recusado. Não havia valor errado — só ordem invertida.
    """
    transacoes = parse_planilha(xlsx(_DECRESCENTE), "santander.xlsx")

    assert [t.data.day for t in transacoes] == [26, 29, 30]
    assert [t.saldo_apos for t in transacoes] == [
        Decimal("250.00"),
        Decimal("400.00"),
        Decimal("300.00"),
    ]


def test_arquivo_de_um_dia_so_nao_e_invertido():
    """Sem duas datas distintas não há o que ordenar, e inverter à toa põe a
    âncora de saldo na ponta errada."""
    linhas = [
        ["Data", "Histórico", "Valor (R$)", "Saldo (R$)"],
        ["01/07/2026", "SALDO ANTERIOR", None, "100,00"],
        ["02/07/2026", "PIX A", "10,00", "110,00"],
        ["02/07/2026", "PIX B", "-30,00", "80,00"],
    ]

    transacoes = parse_planilha(xlsx(linhas), "um_dia.xlsx")

    assert [t.historico for t in transacoes] == ["PIX A", "PIX B"]


# ──────────────────────────────────── quando só o saldo diz a direção

def test_valor_sem_sinal_tem_a_direcao_decidida_pelo_saldo():
    """A poupança do Sicredi imprime `Valor (R$)` sempre positivo.

    `ENCARGOS DE IRRF 0,48` é débito, e nada no valor, no nome da coluna ou na
    posição distingue isso de um rendimento — só o saldo, que cai de 108,34
    para 107,86.
    """
    linhas = [
        ["Movimentação"],
        ["Data", "Histórico", "Valor (R$)", "Saldo"],
        ["31/12/2025", "SALDO ANTERIOR", "106,20", "106,20"],
        ["23/02/2026", "CAPITALIZ. REND. JR", "1,60", "107,80"],
        ["23/02/2026", "CAPITALIZ. REND. CM", "0,54", "108,34"],
        ["23/02/2026", "ENCARGOS DE IRRF", "0,48", "107,86"],
    ]

    transacoes = parse_planilha(xlsx(linhas), "poupanca.xlsx")

    assert [t.valor for t in transacoes] == [
        Decimal("1.60"),
        Decimal("0.54"),
        Decimal("-0.48"),
    ]


def test_o_saldo_so_troca_o_sinal_e_nunca_o_valor():
    """A correção escolhe entre `+x` e `−x`; ela não pode inventar um valor.

    Se o saldo discordasse também do módulo, faltaria lançamento — e isso é
    coisa que a conferência tem de REPROVAR, não remendar em silêncio.
    """
    linhas = [
        ["Data", "Histórico", "Valor (R$)", "Saldo (R$)"],
        ["01/07/2026", "SALDO ANTERIOR", None, "100,00"],
        ["02/07/2026", "PIX ALFA", "10,00", "150,00"],
    ]

    with pytest.raises(PlanilhaParseError, match="não caminha"):
        parse_planilha(xlsx(linhas), "buraco.xlsx")


def test_linha_que_nao_move_o_saldo_nao_e_lancamento():
    """O relatório do Omie.CASH mistura previsto e realizado.

    O pedido de venda `Atrasado` de 72.500,00 tem valor, data e contraparte,
    mas o saldo não sai do lugar: só a coluna `Saldo Previsto` se mexe. É a
    mesma família do rodapé do Itaú que entrou como crédito de R$ 19.070,30.
    """
    linhas = [
        ["Situação", "Data", "Cliente ou Fornecedor", "Valor (R$)", "Saldo (R$)",
         "Saldo Previsto (R$)"],
        ["", "30/06/2026", "SALDO ANTERIOR", "0,00", "1.000,00", "9.000,00"],
        ["Conciliado", "01/07/2026", "ALFA LTDA", "-200,00", "800,00", "8.800,00"],
        ["Atrasado", "31/07/2026", "BETA SA", "72.500,00", "800,00", "81.300,00"],
    ]

    transacoes = parse_planilha(xlsx(linhas), "omie.xlsx")

    assert len(transacoes) == 1
    assert transacoes[0].valor == Decimal("-200.00")


def test_a_coluna_de_saldo_prevista_nao_e_confundida_com_a_realizada():
    """`Saldo (R$)` vem antes de `Saldo Previsto (R$)`, e vale a primeira."""
    linhas = [
        ["Data", "Histórico", "Valor (R$)", "Saldo (R$)", "Saldo Previsto (R$)"],
        ["01/07/2026", "SALDO ANTERIOR", None, "1.000,00", "9.000,00"],
        ["02/07/2026", "ALFA LTDA", "-200,00", "800,00", "8.800,00"],
    ]

    transacoes = parse_planilha(xlsx(linhas), "previsto.xlsx")

    assert transacoes[0].saldo_apos == Decimal("800.00")


# ──────────────────────────────────────── a coluna de data tem de ser data

def test_data_dentro_de_prosa_nao_vira_lancamento():
    """`Saldo a partir 04/05/12:` fica na coluna de data da poupança Sicredi.

    O `parse_data` acha data em qualquer ponto do texto — certo para o PDF, em
    que a linha é corrida. Aqui isso criou um lançamento de 2012; e aquela data
    solta no meio de 2026 ainda fez a planilha parecer decrescente, invertendo
    um arquivo que estava na ordem certa. Um defeito produzindo o outro.
    """
    linhas = [
        ["Data", "Histórico", "Valor (R$)", "Saldo"],
        ["31/12/2025", "SALDO ANTERIOR", "106,20", "106,20"],
        ["23/02/2026", "CAPITALIZ. REND. JR", "1,60", "107,80"],
        ["Saldo a partir 04/05/12:", None, "107,80", None],
        ["Líquido para Saque:", None, "107,80", None],
    ]

    transacoes = parse_planilha(xlsx(linhas), "poupanca.xlsx")

    assert len(transacoes) == 1
    assert transacoes[0].data.year == 2026


def test_data_com_hora_continua_sendo_data():
    """O Grafeno imprime `2026-07-31 04:08:37.908000` na coluna de data."""
    linhas = [
        ["Data_da_Ocorrencia", "Lancamento", "Valor", "Saldo"],
        ["2026-07-31 04:08:37.908000", "Tarifas de conta", "-250,00", "555,68"],
    ]

    transacoes = parse_planilha(xlsx(linhas), "grafeno.xlsx")

    assert transacoes[0].data.isoformat() == "2026-07-31"


# ────────────────────────────────────────────── o que o NEO vai receber

def test_razao_social_e_cnpj_entram_no_historico():
    """A planilha traz `Razão Social` e `CPF/CNPJ` em colunas próprias.

    Não há campo estruturado novo para eles: vão para o histórico, que é onde o
    `documento_no_historico` já procura o CNPJ e onde a contraparte resolve
    pela via que já existe.
    """
    linhas = [
        ["Data", "Lançamento", "Razão Social", "CPF/CNPJ", "Valor (R$)", "Saldo (R$)"],
        ["01/07/2026", "SALDO ANTERIOR", None, None, None, "1.000,00"],
        ["02/07/2026", "PIX ENVIADO", "ALFA COMERCIO LTDA", "41.250.201/0001-24",
         "-100,00", "900,00"],
    ]

    (transacao,) = parse_planilha(xlsx(linhas), "itau.xlsx")

    assert "ALFA COMERCIO LTDA" in transacao.historico
    assert "41.250.201/0001-24" in transacao.historico


def test_o_historico_cabe_na_coluna_do_banco():
    """`Transacao.historico` é VARCHAR(200) aqui e VARCHAR(500) no banco.

    Juntar histórico, razão social, CNPJ e documento numa string só passa fácil
    do limite — e o SQLite dos testes aceita calado enquanto o Postgres recusa.
    """
    linhas = [
        ["Data", "Lançamento", "Razão Social", "Valor (R$)", "Saldo (R$)"],
        ["01/07/2026", "SALDO ANTERIOR", None, None, "1.000,00"],
        ["02/07/2026", "PIX " + "X" * 300, "ALFA " + "Y" * 300, "-100,00", "900,00"],
    ]

    (transacao,) = parse_planilha(xlsx(linhas), "longo.xlsx")

    assert len(transacao.historico) <= 200


# ───────────────────────────────────────────────── formatos do arquivo

def test_xls_que_na_verdade_e_xlsx_e_lido_pela_assinatura():
    """O banco renomeia a extensão e o `xlrd` recusa com "not supported".

    A assinatura ZIP no início do arquivo é o que decide de verdade — o nome é
    palpite do exportador.
    """
    transacoes = parse_planilha(xlsx(_CREDITO_DEBITO), "BRADESCO C&C JUL 26.XLS")

    assert len(transacoes) == 2


def test_csv_com_ponto_e_virgula():
    conteudo = (
        "Data;Histórico;Valor (R$);Saldo (R$)\r\n"
        "01/07/2026;SALDO ANTERIOR;;1.000,00\r\n"
        "02/07/2026;PIX ENVIADO ALFA;-100,00;900,00\r\n"
    ).encode("utf-8")

    (transacao,) = parse_planilha(conteudo, "extrato.csv")

    assert transacao.valor == Decimal("-100.00")
    assert transacao.historico == "PIX ENVIADO ALFA"


def test_csv_terminado_so_por_cr_e_lido():
    """`Bradesco_18052026_141344.CSV` termina as linhas com `\\r` puro.

    São 772 `\\r` sozinhos, 3 CRLF e 6 LF no mesmo arquivo — exportador antigo.
    Sem `newline=""` no fluxo, o terminador entra dentro do campo e o módulo
    `csv` para com "new-line character seen in unquoted field", num arquivo que
    não tem uma única aspa. Eram 729 lançamentos recusados por isso.
    """
    conteudo = (
        "\n;Extrato de: Agência: 49  Conta: 255225-6\r\n"
        "Data;Lançamento;Dcto.;Crédito (R$);Débito (R$);Saldo (R$)\r"
        "30/01/2026;SALDO ANTERIOR;;;;8.176,05\r"
        "02/02/2026;RENTAB.INVEST FACILCRED*;6393379;0,49;;8.176,54\r"
        "02/02/2026;PAGTO ELETRON COBRANCA;3433;;-317,85;7.858,69\r"
    ).encode("latin-1")

    transacoes = parse_planilha(conteudo, "Bradesco_18052026_141344.CSV")

    assert [t.valor for t in transacoes] == [Decimal("0.49"), Decimal("-317.85")]


def test_csv_em_latin1_nao_perde_o_acento():
    conteudo = (
        "Data;Histórico;Valor (R$);Saldo (R$)\r\n"
        "01/07/2026;SALDO ANTERIOR;;1.000,00\r\n"
        "02/07/2026;MANUTENÇÃO;-100,00;900,00\r\n"
    ).encode("latin-1")

    (transacao,) = parse_planilha(conteudo, "extrato.csv")

    assert transacao.historico == "MANUTENÇÃO"


def test_planilha_sem_nenhuma_linha_de_movimento_recusa():
    linhas = [
        ["Data", "Histórico", "Valor (R$)", "Saldo (R$)"],
        ["01/07/2026", "SALDO ANTERIOR", None, "1.000,00"],
    ]

    with pytest.raises(PlanilhaParseError, match="nenhuma linha"):
        parse_planilha(xlsx(linhas), "vazia.xlsx")
