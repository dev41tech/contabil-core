"""Adaptadores de layout por banco (Bradesco e Inter).

As linhas reproduzem a estrutura real extraída pelo pdfplumber, com razão
social, CNPJs e valores fictícios. Nenhum dado de cliente entra no repositório.

Os dois casos que originaram estes testes, medidos contra extratos reais antes
de existir adaptador:

- **Bradesco** extraía 119 lançamentos e nenhum importava. O histórico vem em
  três linhas (tipo acima, dados no meio, contraparte abaixo) e o parser
  genérico lia só a de cima; a de baixo ficava solta e era consumida pelo
  lançamento SEGUINTE. Além disso o arquivo traz dois extratos ("Extrato" e
  "Últimos Lançamentos"), cada um com seu `SALDO ANTERIOR` — conferir a cadeia
  de ponta a ponta acusava um salto na emenda que não era erro.
- **Inter** extraía zero. A data é cabeçalho de dia escrito por extenso e não
  aparece na linha do lançamento.
"""

from decimal import Decimal

import pytest

from src.domain.extrato import bancos
from src.domain.extrato.bancos import bradesco, inter
from src.domain.extrato.pdf_parser import PDFParseError, _validar_blocos

TOLERANCIA = Decimal("0.05")


# ─────────────────────────────────────────────────────────────────── Bradesco

_BRADESCO_CABECALHO = [
    "Agência | Conta Total Disponível (R$) Total (R$)",
    "00780 | 0011111-1 -1.006,72 -1.006,72",
    "Extrato de: Ag: 780 | CC: 0011111-1 | Entre 01/01/2026 e 31/01/2026",
    "Data Lançamento Dcto. Crédito (R$) Débito (R$) Saldo (R$)",
    "31/12/2025 SALDO ANTERIOR 1.000,00",
]

# Três lançamentos: um que se descreve sozinho e dois em grupo de três linhas.
_BRADESCO_LANCAMENTOS = [
    "02/01/2026 RENTAB.INVEST FACILCRED* 3827085 10,00 1.010,00",
    "PIX ENVIADO",
    "1406542 -500,00 510,00",
    "DES: EXEMPLO TRANSPORTES LTDA 02/01",
    "PAGTO ELETRON COBRANCA",
    "05/01/2026 1349 -250,00 260,00",
    "FORNECEDOR EXEMPLO ABC 1234",
]

_BRADESCO_SEGUNDO_EXTRATO = [
    "Últimos Lançamentos",
    "Data Lançamento Dcto. Crédito (R$) Débito (R$) Saldo (R$)",
    "05/06/2026 SALDO ANTERIOR -3.000,00",
    "PIX RECEBIDO",
    "08/06/2026 1101599 900,00 -2.100,00",
    "REM: EXEMPLO COOPERATIVA 08/06",
]


def _bradesco(linhas):
    return bradesco.extrair(_BRADESCO_CABECALHO + linhas, 2026)


def test_bradesco_junta_tipo_e_contraparte_no_mesmo_historico():
    (bloco,) = _bradesco(_BRADESCO_LANCAMENTOS)
    pix = bloco.transacoes[1]
    assert pix.valor == Decimal("-500.00")
    assert pix.historico == "PIX ENVIADO DES: EXEMPLO TRANSPORTES LTDA 02/01"


def test_bradesco_nao_rouba_a_contraparte_para_o_lancamento_seguinte():
    """O defeito original: 'DES: ...' virava o histórico da linha de baixo."""
    (bloco,) = _bradesco(_BRADESCO_LANCAMENTOS)
    seguinte = bloco.transacoes[2]
    assert "EXEMPLO TRANSPORTES" not in seguinte.historico
    assert seguinte.historico == "PAGTO ELETRON COBRANCA FORNECEDOR EXEMPLO ABC 1234"


def test_bradesco_linha_que_se_descreve_sozinha_ignora_o_texto_acima():
    (bloco,) = _bradesco(_BRADESCO_LANCAMENTOS)
    rentab = bloco.transacoes[0]
    assert rentab.historico == "RENTAB.INVEST FACILCRED* 3827085"
    assert rentab.valor == Decimal("10.00")


def test_bradesco_herda_a_data_do_ultimo_lancamento_datado():
    (bloco,) = _bradesco(_BRADESCO_LANCAMENTOS)
    assert [t.data.isoformat() for t in bloco.transacoes] == [
        "2026-01-02",
        "2026-01-02",
        "2026-01-05",
    ]


def test_bradesco_le_o_saldo_anterior_do_bloco():
    (bloco,) = _bradesco(_BRADESCO_LANCAMENTOS)
    assert bloco.saldo_anterior == Decimal("1000.00")


def test_bradesco_separa_os_dois_extratos_do_mesmo_arquivo():
    blocos = _bradesco(_BRADESCO_LANCAMENTOS + _BRADESCO_SEGUNDO_EXTRATO)
    assert len(blocos) == 2
    assert [len(b.transacoes) for b in blocos] == [3, 1]
    assert blocos[1].saldo_anterior == Decimal("-3000.00")


def test_bradesco_dois_blocos_passam_na_validacao_por_bloco():
    """A emenda entre os blocos não é salto: a cadeia vale dentro de cada um."""
    blocos = _bradesco(_BRADESCO_LANCAMENTOS + _BRADESCO_SEGUNDO_EXTRATO)
    assert _validar_blocos(blocos, TOLERANCIA) is True


def test_bradesco_primeiro_lancamento_perdido_e_recusado_pelo_saldo_anterior():
    """Sem o `saldo_anterior` nenhuma cadeia consegue acusar o topo faltando."""
    blocos = _bradesco(_BRADESCO_LANCAMENTOS[1:])
    with pytest.raises(PDFParseError, match="faltando no início"):
        _validar_blocos(blocos, TOLERANCIA)


# ────────────────────────────────────────────────────────────────────── Inter

_INTER = [
    "Solicitado em: 09/06/2026 - 13h08",
    "CPF/CNPJ: 11.111.111/0001-11, Instituição: Banco Inter, Agência: 0001-9",
    "Período: 01/01/2026 a 31/01/2026",
    "2 de Janeiro de 2026 Saldo do dia: R$ 850,00 Valor Saldo por transação",
    'Pix enviado: "Cp :11111111-Fulano de Tal" -R$ 100,00 R$ 900,00',
    'Pix recebido: "Cp :22222222-EXEMPLO LTDA" R$ 50,00 R$ 950,00',
    "Fale com a gente",
    "SAC: 0800 000 0000 (opção 09) Ouvidoria: 0800 000 0001",
    "3 de Janeiro de 2026 Saldo do dia: R$ 700,00",
    'Pix enviado: "Cp :33333333-Beltrano" -R$ 250,00 R$ 700,00',
]


def test_inter_aplica_a_data_do_cabecalho_de_dia_aos_lancamentos_abaixo():
    (bloco,) = inter.extrair(_INTER, 2026)
    assert [t.data.isoformat() for t in bloco.transacoes] == [
        "2026-01-02",
        "2026-01-02",
        "2026-01-03",
    ]


def test_inter_le_o_sinal_antes_do_simbolo_de_moeda():
    (bloco,) = inter.extrair(_INTER, 2026)
    assert [t.valor for t in bloco.transacoes] == [
        Decimal("-100.00"),
        Decimal("50.00"),
        Decimal("-250.00"),
    ]


def test_inter_preserva_a_contraparte_no_historico():
    (bloco,) = inter.extrair(_INTER, 2026)
    assert bloco.transacoes[1].historico == 'Pix recebido: "Cp :22222222-EXEMPLO LTDA"'


def test_inter_ignora_rodape_de_atendimento():
    (bloco,) = inter.extrair(_INTER, 2026)
    assert all("SAC" not in t.historico for t in bloco.transacoes)
    assert len(bloco.transacoes) == 3


def test_inter_cadeia_de_saldos_confere():
    assert _validar_blocos(inter.extrair(_INTER, 2026), TOLERANCIA) is True


def test_inter_lancamento_faltando_quebra_a_cadeia():
    sem_o_meio = [ln for ln in _INTER if "EXEMPLO LTDA" not in ln]
    with pytest.raises(PDFParseError, match="não caminha"):
        _validar_blocos(inter.extrair(sem_o_meio, 2026), TOLERANCIA)


# ──────────────────────────────────────────────────────────────────── registro


def test_registro_resolve_por_sigla_cadastrada_na_agencia():
    assert bancos.por_sigla("bradesco") is bradesco
    assert bancos.por_sigla("237") is bradesco
    assert bancos.por_sigla("INTER") is inter


def test_registro_ignora_banco_sem_adaptador():
    assert bancos.por_sigla("BANCO QUE NAO EXISTE") is None
    assert bancos.por_sigla(None) is None


def test_registro_cai_na_deteccao_por_conteudo_quando_a_sigla_nao_ajuda():
    linhas = _BRADESCO_CABECALHO + _BRADESCO_LANCAMENTOS
    assert bancos.escolher(None, linhas) is bradesco
    assert bancos.escolher(None, _INTER) is inter


def test_registro_prefere_a_sigla_ao_conteudo():
    """Sigla cadastrada ganha: é o cadastro que define o banco, não o arquivo."""
    assert bancos.escolher("INTER", _BRADESCO_CABECALHO) is inter
