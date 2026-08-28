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
    bbc,
    c6,
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
