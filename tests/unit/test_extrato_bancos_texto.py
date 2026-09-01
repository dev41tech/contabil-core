"""Sete adaptadores de extrato com camada de texto.

Fitbank, BBC, Cresol, Sicoob, Nubank, C6 e Mercado Pago. Os dados reproduzem a
estrutura e a geometria reais, com razão social, CNPJs e valores fictícios.
Nenhum dado de cliente entra no repositório.

Cada teste aqui trava um defeito que **falharia em silêncio** — valor com o
sinal trocado ou lançamento a menos, e não erro de execução. Todos foram
encontrados medindo contra extratos reais:

- **Fitbank:** a coluna vazia vem escrita como `-`; é a posição do número que
  diz se é entrada ou saída.
- **BBC, Cresol, Sicoob:** o "Saldo do dia" é o de FECHAMENTO e vem impresso
  ANTES dos lançamentos daquele dia, porque a lista é decrescente. Ancorar no
  primeiro lançamento que aparece embaixo dele desloca a cadeia por um dia
  inteiro de movimento.
- **Cresol:** dia que atravessa a quebra de página tem o cabeçalho repetido —
  abrir um grupo novo ali ancora o mesmo saldo duas vezes.
- **Sicoob:** o sinal é uma letra colada no número (`330,00D`).
- **Nubank:** os valores não têm sinal; quem o define é a seção ("Total de
  entradas" × "Total de saídas").
- **Mercado Pago:** o cabeçalho da tabela não se repete em toda página. Pular a
  página sem cabeçalho perdeu dois dias de lançamentos.
"""

from decimal import Decimal

import pytest

from src.domain.extrato import bancos
from src.domain.extrato.bancos import (
    bb,
    bbc,
    c6,
    caixa,
    cresol,
    fitbank,
    mercadopago,
    nubank,
    santander,
    sicoob,
    sicredi,
)
from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

TOLERANCIA = Decimal("0.05")


def _p(texto: str, x0: float, top: float, largura: float = 0.0) -> dict:
    return {"text": texto, "x0": x0, "x1": x0 + (largura or len(texto) * 3.5), "top": top}


# ────────────────────────────────────────────────────────────────── Fitbank

_FITBANK = [
    "Extrato de Omie.CASH",
    "Período de 01/06/2026 até 30/06/2026 (Página 1/1)",
    "Situação Data Cliente ou Fornecedor Documento Categoria Entradas Saídas Saldo",
    "31/05 SALDO ANTERIOR 19,40",
    "Conciliado 01/06 EXEMPLO CLIENTE Vendas 3.570,00 - 3.589,40",
    "Conciliado 01/06 tar.20260601.163923 Tarifas Bancárias - 1,99 3.587,41",
]


def test_fitbank_le_o_sinal_da_coluna_com_traco_no_lugar_vazio():
    (bloco,) = fitbank.extrair(_FITBANK, 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("3570.00"),
        Decimal("-1.99"),
    ]


def test_fitbank_tira_o_ano_do_periodo_e_nao_do_relogio():
    (bloco,) = fitbank.extrair(_FITBANK, 1999)
    assert all(t.data.year == 2026 for t in bloco.transacoes)


def test_fitbank_a_cadeia_fecha_desde_o_saldo_anterior():
    blocos = fitbank.extrair(_FITBANK, 2026)
    assert blocos[0].saldo_anterior == Decimal("19.40")
    assert _validar_blocos(blocos, TOLERANCIA) is True


# ────────────────────────────────────────────────────────────────────── BBC

_BBC = [
    "Saldo inicial do período: Total de entradas: Total de saídas: Saldo final do período:",
    "R$ 1.000,00 +R$ 500,00 -R$ 300,00 R$ 1.200,00",
    "Movimentações",
    "12 AGO 2026 Saldo do dia: R$ 1.200,00",
    "Transferência entre contas enviada -R$ 100,00",
    "11 AGO 2026 Saldo do dia: R$ 1.300,00",
    "Transferência Pix recebida de EXEMPLO LTDA +R$ 500,00",
    "Transferência entre contas enviada -R$ 200,00",
]


def test_bbc_ancora_o_saldo_do_dia_no_ultimo_lancamento_cronologico():
    (bloco,) = bbc.extrair(_BBC, 2026)
    de_11 = [t for t in bloco.transacoes if t.data.day == 11]
    # Dentro do dia a ordem também inverte: o -200,00 aparece por último no PDF.
    assert [t.valor for t in de_11] == [Decimal("-200.00"), Decimal("500.00")]
    assert de_11[-1].saldo_apos == Decimal("1300.00")
    assert de_11[0].saldo_apos is None


def test_bbc_le_as_duas_pontas_da_capa():
    (bloco,) = bbc.extrair(_BBC, 2026)
    assert bloco.saldo_anterior == Decimal("1000.00")
    assert bloco.saldo_final == Decimal("1200.00")


def test_bbc_a_cadeia_fecha_de_ponta_a_ponta():
    assert _validar_blocos(bbc.extrair(_BBC, 2026), TOLERANCIA) is True


def test_bbc_lancamento_faltando_quebra_a_cadeia():
    sem_um = [ln for ln in _BBC if not ln.startswith("Transferência Pix recebida")]
    with pytest.raises(PDFParseError):
        _validar_blocos(bbc.extrair(sem_um, 2026), TOLERANCIA)


# ─────────────────────────────────────────────────────────────────── Cresol

_CRESOL = [
    "31/12/2025 Saldo do Dia: + R$ 700,00",
    "COMPRAS NO DEBITO CARTAO",
    "31/12/2025 - R$ 300,00",
    "MASTERCARD EXEMPLO COMERCIO",
    "30/12/2025 Saldo do Dia: + R$ 1.000,00",
    "30/12/2025 PIX DEBITO PARA: EXEMPLO LTDA - R$ 500,00",
    "Consulta Posição consolidada em 05/08/2026 às 09:18:14",
    "Página 3 de 5",
    "Lançamentos",
    "30/12/2025 Saldo do Dia: + R$ 1.000,00",
    "30/12/2025 PIX CREDITO DE: OUTRO EXEMPLO + R$ 200,00",
]


def test_cresol_ignora_o_cabecalho_repetido_na_quebra_de_pagina():
    """Dois cabeçalhos de 30/12 com o mesmo saldo: um grupo só, uma âncora só."""
    (bloco,) = cresol.extrair(_CRESOL, 2025)
    de_30 = [t for t in bloco.transacoes if t.data.day == 30]
    assert len(de_30) == 2
    assert [t.saldo_apos for t in de_30] == [None, Decimal("1000.00")]


def test_cresol_monta_a_descricao_das_linhas_em_volta_quando_a_linha_esta_vazia():
    (bloco,) = cresol.extrair(_CRESOL, 2025)
    de_31 = next(t for t in bloco.transacoes if t.data.day == 31)
    assert de_31.historico == "COMPRAS NO DEBITO CARTAO MASTERCARD EXEMPLO COMERCIO"


def test_cresol_usa_a_descricao_da_propria_linha_quando_ela_existe():
    (bloco,) = cresol.extrair(_CRESOL, 2025)
    pix = next(t for t in bloco.transacoes if t.valor == Decimal("-500.00"))
    assert pix.historico == "PIX DEBITO PARA: EXEMPLO LTDA"


def test_cresol_a_cadeia_fecha():
    assert _validar_blocos(cresol.extrair(_CRESOL, 2025), TOLERANCIA) is True


# ─────────────────────────────────────────────────────────────────── Sicoob

_SICOOB = [
    "PERÍODO: 01/07/2025 - 31/07/2025",
    "DATA HISTÓRICO VALOR",
    "31/07 SALDO DO DIA 700,00C",
    "31/07 TARIFA COBRANÇA 300,00D",
    "DOC.: 715129",
    "30/07 SALDO DO DIA 1.000,00C",
    "30/07 PIX REC.OUTRA IF 200,00C",
    "Recebimento Pix",
    "11.111.111 0001-11",
]


def test_sicoob_le_a_letra_colada_no_numero_como_sinal():
    (bloco,) = sicoob.extrair(_SICOOB, 2025)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("200.00"),
        Decimal("-300.00"),
    ]


def test_sicoob_cola_as_linhas_de_detalhe_no_lancamento_de_cima():
    (bloco,) = sicoob.extrair(_SICOOB, 2025)
    pix = next(t for t in bloco.transacoes if t.valor > 0)
    assert "Recebimento Pix" in pix.historico
    assert "11.111.111 0001-11" in pix.historico


def test_sicoob_tira_o_ano_do_periodo():
    (bloco,) = sicoob.extrair(_SICOOB, 1999)
    assert all(t.data.year == 2025 for t in bloco.transacoes)


def test_sicoob_a_cadeia_fecha():
    assert _validar_blocos(sicoob.extrair(_SICOOB, 2025), TOLERANCIA) is True


# ─────────────────────────────────────────────────────────────────── Nubank

_NUBANK = [
    "Saldo inicial 0,00",
    "Total de entradas +45.300,00",
    "Total de saídas -45.250,00",
    "Movimentações",
    "09 OUT 2025 Total de entradas + 300,00",
    "Transferência Recebida Fulano - ***.440.409-** - NU 300,00",
    "PAGAMENTOS - IP (0260) Agência: 1 Conta:",
    "Total de saídas - 250,00",
    "Aplicação RDB 250,00",
    "Saldo do dia 50,00",
    "Saldo final do período 50,00",
]


def test_nubank_tira_o_sinal_da_secao_e_nao_do_numero():
    (bloco,) = nubank.extrair(_NUBANK, 2025)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("300.00"),
        Decimal("-250.00"),
    ]


def test_nubank_ignora_os_totais_da_capa_antes_de_movimentacoes():
    """Na capa os mesmos rótulos são do período e não abrem seção nenhuma."""
    (bloco,) = nubank.extrair(_NUBANK, 2025)
    assert len(bloco.transacoes) == 2
    assert all(abs(t.valor) < Decimal("1000") for t in bloco.transacoes)


def test_nubank_cola_a_continuacao_da_descricao():
    (bloco,) = nubank.extrair(_NUBANK, 2025)
    assert "PAGAMENTOS - IP (0260)" in bloco.transacoes[0].historico


def test_nubank_a_cadeia_fecha_no_saldo_do_dia():
    blocos = nubank.extrair(_NUBANK, 2025)
    assert blocos[0].transacoes[-1].saldo_apos == Decimal("50.00")
    assert _validar_blocos(blocos, TOLERANCIA) is True


def test_nubank_le_as_duas_pontas_da_capa():
    """Sem elas um extrato de um dia só teria uma âncora e nada a conferir."""
    (bloco,) = nubank.extrair(_NUBANK, 2025)
    assert bloco.saldo_anterior == Decimal("0.00")
    assert bloco.saldo_final == Decimal("50.00")


# ─────────────────────────────────────────────────────────────────────── C6

_C6 = [
    "Maio 2025 ( 01/05/2025 - 31/05/2025 ) Entradas: R$ 100,00 · Saídas: R$ 40,00",
    "02/05 02/05 Entrada PIX Pix recebido de Fulano R$ 100,00",
    "02/05 02/05 Pagamento EXEMPLO COMERCIO - ME -R$ 40,00",
    "Saldo do dia 02/05/25 R$ 60,00",
    "Pix enviado para EXEMPLO EQUIPAMENTOS HIDRAULICOS LTDA EM RECUPERACAO",
    "06/05 06/05 Saída PIX -R$ 10,00",
    "JUDICIAL",
    "Saldo do dia 06/05/25 R$ 50,00",
]


def test_c6_usa_a_data_de_lancamento_e_o_ano_do_periodo():
    (bloco,) = c6.extrair(_C6, 1999)
    assert [t.data.isoformat() for t in bloco.transacoes] == [
        "2025-05-02",
        "2025-05-02",
        "2025-05-06",
    ]


def test_c6_junta_a_descricao_quebrada_acima_e_abaixo():
    (bloco,) = c6.extrair(_C6, 2025)
    ultimo = bloco.transacoes[-1]
    assert ultimo.historico.startswith("Pix enviado para EXEMPLO EQUIPAMENTOS")
    assert ultimo.historico.endswith("JUDICIAL")
    assert "Saída PIX" in ultimo.historico


def test_c6_ancora_o_saldo_no_ultimo_lancamento_do_dia():
    (bloco,) = c6.extrair(_C6, 2025)
    assert bloco.transacoes[0].saldo_apos is None
    assert bloco.transacoes[1].saldo_apos == Decimal("60.00")
    assert bloco.transacoes[2].saldo_apos == Decimal("50.00")


def test_c6_a_cadeia_fecha():
    assert _validar_blocos(c6.extrair(_C6, 2025), TOLERANCIA) is True


# ───────────────────────────────────────────────────────────── Mercado Pago

_CABECALHO_MP = [
    _p("Data", 40, 184),
    _p("Descrição", 89, 184),
    _p("ID", 198, 184),
    _p("da", 207, 184),
    _p("operação", 217, 184),
    _p("Valor", 312, 184),
    _p("Saldo", 384, 184),
]

_MP_PAGINA_COM_CABECALHO = [
    *_CABECALHO_MP,
    _p("Pix", 89, 209),
    _p("recebido", 101, 209),
    _p("01-04-2026", 40, 222, largura=43),
    _p("EXEMPLO", 89, 222),
    _p("152067851233", 198, 222),
    _p("R$", 298, 222, largura=9),
    _p("238,08", 309, 222),
    _p("R$", 371, 222, largura=9),
    _p("372,35", 382, 222),
    _p("LTDA", 89, 233),
]

# Página de continuação: a listagem segue SEM repetir o cabeçalho.
_MP_PAGINA_SEM_CABECALHO = [
    _p("02-04-2026", 40, 100, largura=43),
    _p("EXEMPLO", 89, 100),
    _p("152073823577", 198, 100),
    _p("R$", 297, 100, largura=9),
    _p("-100,00", 308, 100),
    _p("R$", 374, 100, largura=9),
    _p("272,35", 385, 100),
]


def test_mercadopago_junta_a_descricao_das_linhas_em_volta():
    (bloco,) = mercadopago.extrair_de_palavras([_MP_PAGINA_COM_CABECALHO], 2026)
    assert bloco.transacoes[0].historico == "Pix recebido EXEMPLO LTDA"


def test_mercadopago_le_pagina_de_continuacao_sem_cabecalho():
    """Pular a página sem cabeçalho perdia dois dias de lançamentos no arquivo real."""
    blocos = mercadopago.extrair_de_palavras(
        [_MP_PAGINA_COM_CABECALHO, _MP_PAGINA_SEM_CABECALHO], 2026
    )
    (bloco,) = blocos
    assert len(bloco.transacoes) == 2
    assert bloco.transacoes[1].valor == Decimal("-100.00")


def test_mercadopago_a_cadeia_fecha_entre_as_paginas():
    blocos = mercadopago.extrair_de_palavras(
        [_MP_PAGINA_COM_CABECALHO, _MP_PAGINA_SEM_CABECALHO], 2026
    )
    assert _validar_blocos(blocos, TOLERANCIA) is True


# ──────────────────────────────────────────────────────────────────── registro


@pytest.mark.parametrize(
    ("sigla", "modulo"),
    [
        ("FITBANK", fitbank),
        ("BBC", bbc),
        ("CRESOL", cresol),
        ("SICOOB", sicoob),
        ("NUBANK", nubank),
        ("C6", c6),
        ("MERCADOPAGO", mercadopago),
    ],
)
def test_registro_resolve_as_siglas(sigla, modulo):
    assert bancos.por_sigla(sigla) is modulo


def test_cresol_nao_reconhece_o_extrato_do_inter():
    """O Inter também imprime "Saldo do dia:", e com sinal nos dias negativos."""
    linhas_do_inter = [
        "2 de Janeiro de 2026 Saldo do dia: -R$ 705,36",
        'Pix enviado: "Cp :11111111-Fulano" -R$ 100,00 R$ 900,00',
    ]
    assert cresol.reconhece(linhas_do_inter) is False


def test_nenhuma_amostra_casa_com_dois_adaptadores():
    """Cada assinatura precisa ser exclusiva — o despacho não pode depender da ordem."""
    cabecalhos = {
        "fitbank": ["Situação Data Cliente ou Fornecedor Categoria Entradas Saídas Saldo"],
        "bbc": ["12 AGO 2026 Saldo do dia: R$ 1.200,00"],
        "cresol": ["31/12/2025 Saldo do Dia: + R$ 700,00"],
        "sicoob": ["DATA HISTÓRICO VALOR"],
        "c6": ["Saldo do dia 02/05/25 R$ 60,00"],
    }
    for esperado, linhas in cabecalhos.items():
        casam = [
            m.__name__.rsplit(".", 1)[-1]
            for m in bancos.ADAPTADORES
            if m.reconhece(linhas)
        ]
        assert casam == [esperado], f"{esperado} ficou ambíguo: {casam}"


# ──────────────────────────────────────────── Omie.CASH, variante de uma coluna

# O mesmo gerador do FitBank, exportando para uma conta Sicredi: sem colunas
# separadas de Entradas/Saídas, só `Valor`. O adaptador é escolhido pela
# assinatura e não pela sigla — a agência pode estar cadastrada como SICREDI.
_OMIE_UMA_COLUNA = [
    "Extrato de Sicredi",
    "Período de 01/07/2026 até 31/07/2026 (Página 1/1)",
    "Situação Data Cliente ou Fornecedor Documento Categoria Valor Saldo",
    "30/06 SALDO ANTERIOR 1.000,00",
    "Conciliado 01/07 EXEMPLO ALIMENTOS LTDA Clientes - Revenda 2.000,00 3.000,00",
    "Conciliado 02/07 TARIFA EXEMPLO Tarifas Bancárias -500,00 2.500,00",
]


def test_omie_uma_coluna_e_reconhecido_mesmo_sem_entradas_e_saidas():
    assert fitbank.reconhece(_OMIE_UMA_COLUNA) is True


def test_omie_uma_coluna_le_o_sinal_do_proprio_numero():
    (bloco,) = fitbank.extrair(_OMIE_UMA_COLUNA, 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("2000.00"),
        Decimal("-500.00"),
    ]


def test_omie_uma_coluna_fecha_a_cadeia():
    blocos = fitbank.extrair(_OMIE_UMA_COLUNA, 2026)
    assert blocos[0].saldo_anterior == Decimal("1000.00")
    assert _validar_blocos(blocos, TOLERANCIA) is True


def test_omie_duas_colunas_continua_funcionando():
    """A variante nova não pode ter quebrado a do FitBank."""
    (bloco,) = fitbank.extrair(_FITBANK, 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("3570.00"),
        Decimal("-1.99"),
    ]


# ──────────────────────────────────────────────────────────────── Santander

_SANTANDER = [
    "Santander Empresas",
    "Períodos: 01/06/2026 a 30/06/2026 Data/Hora: 01/07/2026 as 09h28",
    "Saldo disponível para uso: R$ 60,00",
    "Data Histórico Documento Valor (R$) Saldo (R$)",
    "24/06/2026 Tarifa Avulsa Envio Pix 23/06/2026 -10,00 60,00",
    "Tarifa Mensalidade Pacote Servicos",
    "23/06/2026 -30,00 70,00",
    "MAIO / 2026",
    "11/06/2026 Ted Recebida 10580480000160 100,00 100,00",
]


def test_santander_inverte_para_a_ordem_cronologica():
    (bloco,) = santander.extrair(_SANTANDER, 2026)
    datas = [t.data for t in bloco.transacoes]
    assert datas == sorted(datas)
    assert bloco.transacoes[0].valor == Decimal("100.00")


def test_santander_monta_a_descricao_quebrada_em_volta_da_linha():
    """A linha de dados vem só com data, valor e saldo quando a descrição quebra."""
    (bloco,) = santander.extrair(_SANTANDER, 2026)
    do_meio = next(t for t in bloco.transacoes if t.valor == Decimal("-30.00"))
    assert do_meio.historico == "Tarifa Mensalidade Pacote Servicos MAIO / 2026"


def test_santander_usa_a_descricao_da_propria_linha_quando_ela_existe():
    (bloco,) = santander.extrair(_SANTANDER, 2026)
    tarifa = next(t for t in bloco.transacoes if t.valor == Decimal("-10.00"))
    assert tarifa.historico == "Tarifa Avulsa Envio Pix 23/06/2026"


def test_santander_a_cadeia_fecha():
    assert _validar_blocos(santander.extrair(_SANTANDER, 2026), TOLERANCIA) is True


def test_santander_nao_confunde_com_o_cabecalho_do_sicredi():
    """Os dois cabeçalhos só diferem em `Histórico` × `Descrição`."""
    do_sicredi = ["Data Descricao Documento Valor (R$) Saldo (R$)"]
    assert santander.reconhece(do_sicredi) is False
    assert santander.reconhece(["Data Histórico Documento Valor (R$) Saldo (R$)"]) is True


# ───────────────────────────────────────── Sicredi — relatório da cooperativa

# Geometria medida: DATA x0=44, DOCUMENTO x0=81, HISTORICO x0=126,
# bordas direitas DEBITO≈401, CREDITO≈482, SALDO≈565.
_CABECALHO_SICREDI = [
    _p("DATA", 44, 122),
    _p("DOCUMENTO", 81, 122),
    _p("HISTORICO", 126, 122),
    _p("DEBITO", 379, 122, largura=22),
    _p("CREDITO", 456, 122, largura=26),
    _p("SALDO", 546, 122, largura=19),
]


def _direita_sic(texto: str, x1: float, top: float) -> dict:
    return {"text": texto, "x0": x1 - len(texto) * 3.5, "x1": x1, "top": top}


_SICREDI_COOP = [
    *_CABECALHO_SICREDI,
    # Abertura, com as letras separadas
    _p("**/**/****", 32, 136),
    *[_p(letra, 126 + i * 8, 136) for i, letra in enumerate("SALDOANTERIOR")],
    _direita_sic("0,00", 565, 136),
    # Débito sem saldo na linha
    _p("09/01/2024", 32, 143, largura=43),
    _p("PIX_DEB", 81, 143),
    _p("PAGAMENTO", 126, 143),
    _direita_sic("150,00", 401, 143),
    # Crédito com saldo
    _p("09/01/2024", 32, 150, largura=43),
    _p("CAPTACAO", 81, 150),
    _p("RESG.APLIC", 126, 150),
    _direita_sic("150,00", 482, 150),
    _direita_sic("0,00", 565, 150),
]


def test_sicredi_coop_tira_o_sinal_da_coluna_e_nao_do_numero():
    """Os dois lançamentos têm 150,00; só a coluna diz qual é saída."""
    (bloco,) = sicredi.extrair_de_palavras([_SICREDI_COOP], 2024)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-150.00"),
        Decimal("150.00"),
    ]


def test_sicredi_coop_le_a_abertura_com_letras_separadas():
    (bloco,) = sicredi.extrair_de_palavras([_SICREDI_COOP], 2024)
    assert bloco.saldo_anterior == Decimal("0.00")
    assert all("ANTERIOR" not in t.historico.upper() for t in bloco.transacoes)


def test_sicredi_coop_a_cadeia_fecha_por_segmento():
    """O saldo só aparece em algumas linhas, como no Itaú."""
    blocos = sicredi.extrair_de_palavras([_SICREDI_COOP], 2024)
    assert blocos[0].transacoes[0].saldo_apos is None
    assert _validar_blocos(blocos, TOLERANCIA) is True


def test_sicredi_devolve_vazio_no_outro_layout_para_o_generico_assumir():
    """O layout `Data Descrição Documento Valor Saldo` é do parser genérico.

    Reivindicar a sigla SICREDI não pode tirar de funcionamento o que já
    funcionava: quando o arquivo não é o relatório da cooperativa, este
    adaptador devolve lista vazia e o `parse_pdf` segue adiante sozinho.
    """
    outro_layout = [_p("Data", 20, 100), _p("Descricao", 60, 100), _p("Saldo", 300, 100)]
    assert sicredi.extrair_de_palavras([outro_layout], 2026) == []
    assert sicredi.reconhece(["Data Descricao Documento Valor (R$) Saldo (R$)"]) is False


# ────────────────────────────────────────────────────────────────────── Caixa

# Geometria medida: Data/Hora x0=25, Descrição x0=147,
# bordas direitas valor≈447 e saldo≈565; a letra D/C vem ~6 pontos à direita.
_CABECALHO_CAIXA = [
    _p("Data/Hora", 25, 271),
    _p("Nr.", 95, 271),
    _p("Doc.", 110, 271),
    _p("Descrição/Detalhamento", 147, 271),
    _p("Valor", 406, 271),
    _p("(R$)", 432, 271, largura=15),
    _p("Saldo(R$)", 527, 271, largura=38),
]


def _caixa_linha(top, data=None, hora=None, desc=(), valor=None, dc="D",
                 saldo=None, saldo_dc="D"):
    palavras = []
    if data:
        palavras.append(_p(data, 25, top, largura=43))
    if hora:
        palavras.append(_p(hora, 25, top + 9, largura=34))
    for i, texto in enumerate(desc):
        palavras.append(_p(texto, 147 + i * 40, top, largura=38))
    if valor:
        palavras.append({"text": valor, "x0": 410, "x1": 440, "top": top + 4})
        palavras.append({"text": dc, "x0": 443, "x1": 449, "top": top + 4})
    if saldo:
        palavras.append({"text": saldo, "x0": 537, "x1": 558, "top": top + 4})
        palavras.append({"text": saldo_dc, "x0": 563, "x1": 569, "top": top + 4})
    return palavras


_CAIXA = [
    *_CABECALHO_CAIXA,
    *_caixa_linha(292, data="04/08/2025", hora="14:36:09", desc=("DEB", "PIX"),
                  valor="3.797,58", dc="D", saldo="592,08", saldo_dc="D"),
    *_caixa_linha(317, data="05/08/2025", desc=("RESGATE",),
                  valor="592,08", dc="C", saldo="0,00", saldo_dc="C"),
    # Cheque depositado: credita e o saldo NÃO anda — não é movimento
    *_caixa_linha(342, data="13/08/2025", desc=("DEPOSITO", "CHEQUE"),
                  valor="5.110,00", dc="C", saldo="0,00", saldo_dc="C"),
    # A liberação, dias depois: aqui o saldo se move
    *_caixa_linha(367, data="15/08/2025", desc=("DESBLOQ", "CHEQUE"),
                  valor="5.110,00", dc="C", saldo="5.110,00", saldo_dc="C"),
]


def test_caixa_junta_as_tres_alturas_num_lancamento_so():
    """Data, valores e hora ficam a ~4,6 pontos; o lançamento seguinte a ~25."""
    (bloco,) = caixa.extrair_de_palavras([_CAIXA], 2025)
    primeiro = bloco.transacoes[0]
    assert primeiro.data.isoformat() == "2025-08-04"
    assert primeiro.valor == Decimal("-3797.58")
    assert primeiro.saldo_apos == Decimal("-592.08")


def test_caixa_le_o_sinal_da_letra_separada_do_numero():
    (bloco,) = caixa.extrair_de_palavras([_CAIXA], 2025)
    assert [t.valor for t in bloco.transacoes][:2] == [
        Decimal("-3797.58"),
        Decimal("592.08"),
    ]


def test_caixa_nao_importa_o_cheque_depositado_que_nao_moveu_o_saldo():
    """Importar depósito e desbloqueio dobraria o valor no razão."""
    (bloco,) = caixa.extrair_de_palavras([_CAIXA], 2025)
    historicos = " ".join(t.historico.upper() for t in bloco.transacoes)
    assert "DEPOSITO CHEQUE" not in historicos
    assert "DESBLOQ CHEQUE" in historicos
    de_5110 = [t for t in bloco.transacoes if t.valor == Decimal("5110.00")]
    assert len(de_5110) == 1


def test_caixa_a_cadeia_fecha_sem_a_linha_bloqueada():
    """Com o depósito dentro, ela acusaria 5.110,00 de diferença que não existe."""
    blocos = caixa.extrair_de_palavras([_CAIXA], 2025)
    assert _validar_blocos(blocos, TOLERANCIA) is True


# ───────────────────────────────────────────── Banco do Brasil — dois layouts

# Layout B ("Dia Lote"): cada lançamento em três alturas, sinal em (+)/(-).
_CABECALHO_BB_DIA = [
    _p("Dia", 30, 103),
    _p("Lote", 91, 103),
    _p("Documento", 152, 103),
    _p("Histórico", 266, 103),
    _p("Valor", 547, 103, largura=22),
]


def _bb_grupo(top, data=None, hist=(), lote=None, valor=None, sinal="-", compl=()):
    palavras = []
    if data:
        palavras.append(_p(data, 30, top, largura=43))
    for i, texto in enumerate(hist):
        palavras.append(_p(texto, 266 + i * 42, top, largura=40))
    if lote:
        palavras.append(_p(lote, 91, top + 5, largura=24))
    if valor:
        palavras.append({"text": valor, "x0": 536, "x1": 557, "top": top + 5})
        palavras.append({"text": f"({sinal})", "x0": 561, "x1": 573, "top": top + 5})
    for i, texto in enumerate(compl):
        palavras.append(_p(texto, 266 + i * 42, top + 10, largura=40))
    return palavras


# Os grupos ficam a 24 pontos uns dos outros: dentro do grupo as alturas somam
# 10, e o agrupamento usa 14 — precisa separar do grupo seguinte.
_BB_DIA_LOTE = [
    *_CABECALHO_BB_DIA,
    *_bb_grupo(121, hist=("Saldo", "Anterior"), valor="13,44", sinal="-"),
    *_bb_grupo(145, data="02/01/2026", hist=("Cobrança", "de I.O.F."),
               lote="13601", valor="0,29", sinal="-", compl=("IOF", "Saldo Devedor")),
    *_bb_grupo(175, data="00/00/0000", hist=("Saldo", "do dia"),
               lote="13113", valor="13,73", sinal="-"),
]


def test_bb_dia_lote_junta_as_tres_alturas():
    (bloco,) = bb.extrair_de_palavras([_BB_DIA_LOTE], 2026)
    assert len(bloco.transacoes) == 1
    assert bloco.transacoes[0].valor == Decimal("-0.29")


def test_bb_dia_lote_le_o_sinal_entre_parenteses():
    (bloco,) = bb.extrair_de_palavras([_BB_DIA_LOTE], 2026)
    assert bloco.saldo_anterior == Decimal("-13.44")


def test_bb_dia_lote_usa_saldo_do_dia_como_ancora_e_ignora_a_data_invalida():
    """A data `00/00/0000` é do MARCADOR de saldo, não de um lançamento."""
    (bloco,) = bb.extrair_de_palavras([_BB_DIA_LOTE], 2026)
    assert bloco.transacoes[-1].saldo_apos == Decimal("-13.73")
    assert all(t.data.isoformat() == "2026-01-02" for t in bloco.transacoes)


def test_bb_dia_lote_a_cadeia_fecha():
    blocos = bb.extrair_de_palavras([_BB_DIA_LOTE], 2026)
    assert _validar_blocos(blocos, TOLERANCIA) is True


# Layout A ("Dt. balancete"): uma linha por lançamento, sinal em D/C.
_CABECALHO_BB_BAL = [
    _p("Dt.", 60, 195),
    _p("balancete", 73, 195),
    _p("Dt.", 118, 195),
    _p("movimento", 132, 195),
    _p("Lote", 228, 195),
    _p("Histórico", 250, 195),
    _p("Documento", 407, 195),
    _p("Valor", 471, 195),
    _p("R$", 494, 195, largura=8),
    _p("Saldo", 525, 195, largura=18),
]

_BB_BALANCETE = [
    *_CABECALHO_BB_BAL,
    _p("30/01/2026", 65, 206, largura=43),
    _p("Saldo", 266, 206),
    _p("Anterior", 289, 206),
    {"text": "100,00", "x0": 507, "x1": 535, "top": 206},
    {"text": "D", "x0": 542, "x1": 548, "top": 206},
    _p("02/02/2026", 65, 218, largura=43),
    _p("Pix", 266, 218),
    _p("Recebido", 284, 218),
    {"text": "350,00", "x0": 471, "x1": 492, "top": 218},
    {"text": "C", "x0": 499, "x1": 505, "top": 218},
    # A letra do sinal do VALOR grudada no número do SALDO: `D0,00`.
    # -100,00 (abertura) + 350,00 - 250,00 = 0,00, que é o saldo impresso.
    _p("02/02/2026", 65, 242, largura=43),
    _p("BB", 266, 242),
    _p("Rende", 284, 242),
    {"text": "250,00", "x0": 471, "x1": 492, "top": 242},
    {"text": "D0,00", "x0": 499, "x1": 535, "top": 242},
    {"text": "C", "x0": 542, "x1": 548, "top": 242},
]


def test_bb_balancete_separa_a_letra_grudada_no_numero_seguinte():
    """`D0,00` é o sinal do valor mais o saldo — dois campos num token só.

    Sem separar, o saldo se perde e o valor fica sem sinal. No extrato real
    isso escondia sete lançamentos e derrubava a cadeia.
    """
    (bloco,) = bb.extrair_de_palavras([_BB_BALANCETE], 2026)
    sweep = bloco.transacoes[-1]
    assert sweep.valor == Decimal("-250.00")
    assert sweep.saldo_apos == Decimal("0.00")


def test_bb_balancete_le_a_abertura_da_coluna_de_saldo():
    (bloco,) = bb.extrair_de_palavras([_BB_BALANCETE], 2026)
    assert bloco.saldo_anterior == Decimal("-100.00")


def test_bb_balancete_a_cadeia_fecha():
    blocos = bb.extrair_de_palavras([_BB_BALANCETE], 2026)
    assert _validar_blocos(blocos, TOLERANCIA) is True


# ── Sicoob: o export novo do Internet Banking (SISBR) ────────────────────────
#
# Difere do layout de cima em quatro pontos, todos medidos nos extratos reais
# de jan/2026 e fev/2026 da PAULO EDSON DE OLIVEIRA JUNIOR TRANSPORTES:
#
#   - `Periodo:` sem acento, e o cabeçalho ganha a coluna `Documento`;
#   - o valor vem com `R$` na frente: `R$ 600,00D`;
#   - `SALDO DO DIA` fecha o dia por BAIXO, não por cima;
#   - `SALDO ANTERIOR` aparece no rodapé, com data do ano anterior (`31/12`).

_SICOOB_SISBR = [
    "Periodo: 01/01/2026 - 31/01/2026",
    "Data Documento Histórico Valor",
    "30/01 Pix PIX EMITIDO OUTRA IF R$ 600,00D",
    "Pagamento Pix ***.674.379-**",
    "30/01 SALDO DO DIA R$ 400,00C",
    "02/01 Pix PIX RECEBIDO - OUTRA IF R$ 1.000,00C",
    "Recebimento Pix FULANO 11.111.111 0001-11",
    "02/01 SALDO DO DIA R$ 1.000,00C",
    "31/12 SALDO ANTERIOR R$ 0,00C",
    "RESUMO",
    "Saldo em conta: 400,00C",
]


def test_sisbr_le_o_valor_com_cifrao_e_a_coluna_documento():
    """O `R$` entra no grupo do VALOR; fora dele o saldo do dia não casava."""
    (bloco,) = sicoob.extrair(_SICOOB_SISBR, 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("1000.00"), Decimal("-600.00")
    ]
    assert sicoob.reconhece(_SICOOB_SISBR) is True


def test_sisbr_amarra_o_saldo_do_dia_pela_data_e_nao_pela_posicao():
    """Aqui o `SALDO DO DIA` fecha o dia por baixo; no layout antigo, por cima.

    Amarrado à posição, o adaptador jogava os lançamentos de 02/01 no balde do
    dia 30/01 e a cadeia acusava um buraco do tamanho de um dia inteiro. A
    linha de saldo traz a própria data — é ela que decide.
    """
    (bloco,) = sicoob.extrair(_SICOOB_SISBR, 2026)
    por_data = {t.data.isoformat(): t.saldo_apos for t in bloco.transacoes}
    assert por_data == {"2026-01-02": Decimal("1000.00"),
                        "2026-01-30": Decimal("400.00")}


def test_sisbr_saldo_anterior_nao_vira_lancamento():
    """O defeito que a CADEIA DE SALDOS NÃO PEGA.

    `SALDO ANTERIOR` casava com o padrão de lançamento e entrava como um
    crédito. A conferência fechava mesmo assim, porque o valor falso é
    exatamente o saldo de abertura e está na primeira posição — a soma dava
    certo. Quem pegou foi o OFX do mesmo período, com um lançamento a menos.
    """
    (bloco,) = sicoob.extrair(_SICOOB_SISBR, 2026)
    assert len(bloco.transacoes) == 2
    assert all("SALDO ANTERIOR" not in t.historico for t in bloco.transacoes)
    assert bloco.saldo_anterior == Decimal("0.00")


def test_sisbr_data_fora_do_periodo_e_do_ano_anterior():
    """`31/12` num extrato de janeiro/2026 é de 2025, não de 2026."""
    from datetime import date

    from src.domain.extrato.bancos.sicoob import extrair

    linhas = [*_SICOOB_SISBR[:2], "31/12 Pix PIX EMITIDO OUTRA IF R$ 10,00D",
              "31/12 SALDO DO DIA R$ 0,00C"]
    (bloco,) = extrair(linhas, 2026)
    assert bloco.transacoes[0].data == date(2025, 12, 31)


def test_sisbr_a_cadeia_fecha():
    assert _validar_blocos(sicoob.extrair(_SICOOB_SISBR, 2026), TOLERANCIA) is True
