"""Adaptadores da Stone e da Grafeno — os dois extratos emitidos de trás para frente.

Stone e Grafeno listam do lançamento mais recente para o mais antigo. Isso não é
detalhe de apresentação: `ordem` é o que desempata lançamentos do mesmo dia na
tela e no arquivo exportado (ver `ordenacao.py`), e a cadeia de saldos só fecha
lendo do primeiro lançamento para o último. Sem inverter, as duas coisas saem
trocadas — e a cadeia acusaria uma quebra que não existe.

Os dados abaixo reproduzem a geometria e a estrutura reais, com razão social,
CNPJs e valores fictícios. Nenhum dado de cliente entra no repositório.

Os dois achados que estes testes travam:

1. **Stone:** o valor não tem sinal — `226,00` é débito porque a coluna TIPO diz
   "Débito". Lido sem isso, todo lançamento vira crédito.
2. **Stone:** num Pix com tarifa, as DUAS linhas trazem o mesmo saldo, que é o
   do par já líquido. Tratado como saldo por linha, o crédito sozinho não fecha
   e a cadeia quebra por exatamente o valor da tarifa.
"""

from decimal import Decimal

import pytest

from src.domain.extrato import bancos
from src.domain.extrato.bancos import grafeno, stone
from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

TOLERANCIA = Decimal("0.05")


def _p(texto: str, x0: float, top: float, largura: float = 0.0) -> dict:
    return {
        "text": texto,
        "x0": x0,
        "x1": x0 + (largura or len(texto) * 3.5),
        "top": top,
    }


def _direita(texto: str, x1: float, top: float) -> dict:
    return {"text": texto, "x0": x1 - len(texto) * 3.5, "x1": x1, "top": top}


# ───────────────────────────────────────────────────────────────────── Stone

# Geometria medida: DATA x0=37, TIPO x0=86, LANÇAMENTO x0=142,
# VALOR (R$) borda direita 347, SALDO (R$) borda direita 458.
_CABECALHO_STONE = [
    _p("DATA", 37, 180),
    _p("TIPO", 86, 180),
    _p("LANÇAMENTO", 142, 180),
    _p("VALOR", 294, 180),
    _p("(R$)", 332, 180, largura=15),
    _p("SALDO", 406, 180),
    _p("(R$)", 443, 180, largura=15),
    _p("CONTRAPARTE", 477, 180),
]

_STONE = [
    # Cabeçalho da página: titular, acima da tabela — não é contraparte.
    _p("Titular", 20, 120),
    _p("EXEMPLO", 60, 120),
    _p("INDUSTRIA", 95, 120),
    *_CABECALHO_STONE,
    # Grupo 1 (o mais recente): nome quebrado acima + inline, meio de pagamento abaixo
    _p("CALPIE", 142, 210),
    _p("PINTURAS", 174, 210),
    _p("31/07/2025", 26, 222, largura=52),
    _p("Débito", 86, 222),
    _p("INDUSTRIAIS", 142, 222),
    _p("LTDA", 201, 222),
    _direita("226,00", 347, 222),
    _direita("64,73", 458, 222),
    _p("Transferência", 142, 234),
    _p("|", 199, 234),
    _p("Pix", 203, 234),
    # Grupo 2: crédito
    _p("EXEMPLO", 142, 258),
    _p("COMERCIO", 180, 258),
    _p("31/07/2025", 26, 270, largura=52),
    _p("Crédito", 86, 270),
    _direita("288,00", 347, 270),
    _direita("290,73", 458, 270),
    _p("Transferência", 142, 282),
    # Grupo 3: par Pix + tarifa, ambos com o MESMO saldo impresso
    _p("30/07/2025", 26, 306, largura=52),
    _p("Débito", 86, 306),
    _p("Tarifa", 142, 306),
    _direita("-0,95", 347, 306),
    _direita("2,73", 458, 306),
    _p("30/07/2025", 26, 330, largura=52),
    _p("Crédito", 86, 330),
    _p("FULANO", 142, 330),
    _direita("126,99", 347, 330),
    _direita("2,73", 458, 330),
    # Rodapé, longe de qualquer lançamento
    _p("Informações", 142, 700),
    _p("do", 190, 700),
    _p("Comprovante", 200, 700),
]


def _stone():
    (bloco,) = stone.extrair_de_palavras([_STONE], 2025)
    return bloco


def test_stone_tira_o_sinal_da_coluna_tipo():
    bloco = _stone()
    por_historico = {t.historico: t.valor for t in bloco.transacoes}
    assert por_historico["CALPIE PINTURAS INDUSTRIAIS LTDA Transferência | Pix"] == (
        Decimal("-226.00")
    )
    assert por_historico["EXEMPLO COMERCIO Transferência"] == Decimal("288.00")


def test_stone_inverte_para_a_ordem_cronologica():
    bloco = _stone()
    datas = [t.data for t in bloco.transacoes]
    assert datas == sorted(datas)
    assert bloco.transacoes[0].data.isoformat() == "2025-07-30"
    assert bloco.transacoes[-1].data.isoformat() == "2025-07-31"


def test_stone_monta_o_nome_na_ordem_de_leitura():
    """Fragmento de cima entra ANTES da parte que está na linha de dados."""
    bloco = _stone()
    assert any(
        t.historico.startswith("CALPIE PINTURAS INDUSTRIAIS LTDA")
        for t in bloco.transacoes
    )


def test_stone_nao_cola_o_rodape_da_pagina_em_nenhum_lancamento():
    bloco = _stone()
    assert all("Comprovante" not in t.historico for t in bloco.transacoes)


def test_stone_nao_cola_o_cabecalho_da_pagina_no_primeiro_lancamento():
    bloco = _stone()
    assert all("Titular" not in t.historico for t in bloco.transacoes)


def test_stone_saldo_repetido_ancora_so_no_fim_do_grupo():
    """No par Pix+tarifa as duas linhas trazem o mesmo saldo, que é o do par."""
    bloco = _stone()
    do_par = [t for t in bloco.transacoes if t.data.isoformat() == "2025-07-30"]
    assert [t.saldo_apos for t in do_par] == [None, Decimal("2.73")]


def test_stone_a_cadeia_fecha_com_o_par_somado():
    blocos = stone.extrair_de_palavras([_STONE], 2025)
    assert _validar_blocos(blocos, TOLERANCIA) is True


# ─────────────────────────────────────────────────────────────────── Grafeno

_GRAFENO = [
    "Extrato Detalhado",
    "Gerado em: 03/08/2026, às 11:17",
    "CNPJ: 11.111.111/0001-11 Banco Agência Conta R$ 0,00",
    "Período: 01/07/2026 a 31/07/2026",
    "DATA / HORA LANÇAMENTO NOME · DOC · BANCO / AG / CONTA VALOR (R$) SALDO (R$)",
    "31/07/2026 SALDO FINAL R$ 555,68",
    "31/07/2026 Tarifas de conta -R$250,00 R$ 555,68",
    "04:08",
    "16/07/2026 PIX Enviado EXEMPLO COBRANCA LTDA -R$200.000,00 R$ 805,68",
    "10:44 22.222.222/0001-22 · Bco 237 · Ag 5755 · Cc 00252250-0",
    "04:32 Recebimento de boletos +R$200.000,00 R$ 200.805,68",
    "01/07/2026 SALDO INICIAL R$ 805,68",
    "Grafeno · meajuda@grafeno.digital · (11) 3181-6110 Página 1 de 1",
]


def test_grafeno_inverte_para_a_ordem_cronologica():
    (bloco,) = grafeno.extrair(_GRAFENO, 2026)
    datas = [t.data for t in bloco.transacoes]
    assert datas == sorted(datas)
    assert bloco.transacoes[0].historico.startswith("Recebimento de boletos")


def test_grafeno_linha_que_comeca_por_hora_herda_a_data_de_cima():
    (bloco,) = grafeno.extrair(_GRAFENO, 2026)
    recebimento = bloco.transacoes[0]
    assert recebimento.data.isoformat() == "2026-07-16"
    assert recebimento.valor == Decimal("200000.00")


def test_grafeno_cola_o_documento_da_contraparte_no_historico():
    """A linha de continuação traz o CNPJ — é o que resolve a contraparte."""
    (bloco,) = grafeno.extrair(_GRAFENO, 2026)
    pix = next(t for t in bloco.transacoes if "PIX Enviado" in t.historico)
    assert "22.222.222/0001-22" in pix.historico


def test_grafeno_le_as_duas_pontas_da_cadeia():
    (bloco,) = grafeno.extrair(_GRAFENO, 2026)
    assert bloco.saldo_anterior == Decimal("805.68")
    assert bloco.saldo_final == Decimal("555.68")


def test_grafeno_ignora_cabecalho_e_rodape():
    (bloco,) = grafeno.extrair(_GRAFENO, 2026)
    assert len(bloco.transacoes) == 3
    assert all("meajuda" not in t.historico for t in bloco.transacoes)


def test_grafeno_a_cadeia_fecha_de_ponta_a_ponta():
    assert _validar_blocos(grafeno.extrair(_GRAFENO, 2026), TOLERANCIA) is True


def test_grafeno_lancamento_faltando_quebra_a_cadeia():
    sem_a_tarifa = [ln for ln in _GRAFENO if "-R$250,00" not in ln]
    with pytest.raises(PDFParseError):
        _validar_blocos(grafeno.extrair(sem_a_tarifa, 2026), TOLERANCIA)


# ──────────────────────────────────────────────────────────────────── registro


def test_registro_resolve_as_siglas_novas():
    assert bancos.por_sigla("STONE") is stone
    assert bancos.por_sigla("grafeno") is grafeno


def test_deteccao_nao_confunde_stone_grafeno_e_itau():
    cabecalho_stone = "DATA TIPO LANÇAMENTO VALOR (R$) SALDO (R$) CONTRAPARTE"
    cabecalho_grafeno = (
        "DATA / HORA LANÇAMENTO NOME · DOC · BANCO / AG / CONTA VALOR (R$) SALDO (R$)"
    )
    cabecalho_itau = "Data Lançamentos Razão Social CNPJ/CPF Valor (R$) Saldo (R$)"

    assert bancos.por_conteudo([cabecalho_stone]) is stone
    assert bancos.por_conteudo([cabecalho_grafeno]) is grafeno
    assert bancos.por_conteudo([cabecalho_itau]) is not stone
    assert bancos.por_conteudo([cabecalho_itau]) is not grafeno
