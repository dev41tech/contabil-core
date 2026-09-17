"""Leitura do razão em PDF, pelo texto já extraído das páginas.

As linhas reproduzem o layout do relatório RAZÃO do Mister Contador, com valores
fictícios. Os dois defeitos travados aqui vieram do razão da conta de aplicação
da BLD (2025), que a planilha lia inteiro e o PDF recusava:

1. O saldo zerado sai sem D/C ("... 0,59 0,00"). A linha não era reconhecida,
   e o valor dela caía no lançamento seguinte — calculado pela diferença de
   saldo. 17 linhas e R$ 550.668,30 fora dos totais.
2. "Total do mês", o rodapé "Sistema licenciado" e as assinaturas depois do
   "Total da conta" eram colados no histórico do último lançamento.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.domain.conciliacao_bancaria.razao import (
    RazaoInvalido,
    _conferir_total,
    _razao_das_linhas,
)

D = Decimal

_CABECALHO = [
    "Empresa: EXEMPLO LOGISTICA LTDA Folha: 0001",
    "C.N.P.J.: 12.345.678/0001-95",
    "Período: 01/01/2025 - 28/02/2025",
    "CONSOLIDADO",
    "RAZÃO",
    "Data LoteHistórico Cta.C.Part. Débito Crédito Saldo-Exercício",
    "Conta: 2700 - 1.1.1.03.0016 APLICAÇÃO - BANCO",
    "SALDO ANTERIOR 0,00",
]

_APLICACAO = [
    *_CABECALHO,
    "02/01/2025 3001 APLIC AUT MAIS 2699 1.000,00 1.000,00D",
    "03/01/2025 3002 RESGATE DE APLICAÇÃO AUTOMÁTICA 2699 999,41 0,59D",
    "03/01/2025 3003 VALOR REF IRRF DE APLICAÇÃO 1352 0,59 0,00",
    "10/01/2025 3004 OPERAÇÃO DE APLICAÇÃO AUTOMÁTICA 2699 500,00 500,00D",
    "Total do mês: 1.500,00 1.000,00",
    "Sistema licenciado para EXEMPLO CONTABILIDADE LTDA",
    # página seguinte
    "Empresa: EXEMPLO LOGISTICA LTDA Folha: 0002",
    "Conta: 2700 - 1.1.1.03.0016 APLICAÇÃO - BANCO",
    "03/02/2025 3005 RESGATE DE APLICAÇÃO AUTOMÁTICA 2699 500,00 0,00",
    "Total do mês: 0,00 500,00",
    "Total da conta: 1.500,00 1.500,00",
    "_______________________________________",
    "FULANO DE TAL SOCIO ADMINISTRADOR",
    "CPF: 000.000.000-00",
]


def test_saldo_zerado_sem_dc_nao_perde_o_lancamento():
    razao = _razao_das_linhas(_APLICACAO)
    assert [lanc.valor for lanc in razao.lancamentos] == [
        D("1000.00"), D("-999.41"), D("-0.59"), D("500.00"), D("-500.00")
    ]


def test_o_lancamento_seguinte_ao_saldo_zerado_fica_com_o_proprio_valor():
    """Antes, a aplicação de 500,00 saía como 499,41: herdava os 0,59 perdidos."""
    razao = _razao_das_linhas(_APLICACAO)
    assert razao.lancamentos[3].valor == D("500.00")


def test_a_leitura_fecha_com_o_total_da_conta():
    razao = _razao_das_linhas(_APLICACAO)
    _conferir_total(razao)
    assert (razao.total_debito, razao.total_credito) == (D("1500.00"), D("1500.00"))


def test_totais_do_mes_rodape_e_assinaturas_nao_entram_no_historico():
    razao = _razao_das_linhas(_APLICACAO)
    historicos = " | ".join(lanc.historico for lanc in razao.lancamentos)
    assert "Total" not in historicos
    assert "licenciado" not in historicos
    assert "CPF" not in historicos
    assert "SOCIO" not in historicos
    assert razao.lancamentos[-1].historico == "RESGATE DE APLICAÇÃO AUTOMÁTICA"


def test_historico_quebrado_em_duas_linhas_continua_sendo_colado():
    linhas = [
        *_CABECALHO,
        "02/01/2025 3001 PAGAMENTO FORNECEDOR 2699 100,00 100,00D",
        "EXEMPLO TRANSPORTES LTDA",
        "Total da conta: 100,00 0,00",
    ]
    (lancamento,) = _razao_das_linhas(linhas).lancamentos
    assert lancamento.historico == "PAGAMENTO FORNECEDOR EXEMPLO TRANSPORTES LTDA"


def test_linha_perdida_continua_sendo_recusada_pelo_total():
    sem_uma = [linha for linha in _APLICACAO if not linha.startswith("10/01/2025")]
    razao = _razao_das_linhas(sem_uma)
    with pytest.raises(RazaoInvalido, match="não fecha"):
        _conferir_total(razao)
