"""Layout de extrato do Sicredi (conta corrente, camada determinística).

As linhas abaixo reproduzem a estrutura real extraída pelo pdfplumber — cabeçalho
do associado quebrado em duas linhas, cabeçalho de coluna, linha "SALDO" de
abertura e lançamentos `data descrição [documento] valor saldo` — com razão
social, CNPJs e valores fictícios. Nenhum dado de cliente entra no repositório.

O caso que originou estes testes: em fevereiro/2026 a conta estava com saldo
devedor o mês inteiro. Como o grupo do saldo em `_TX_LINE` não aceitava sinal
negativo, NENHUMA linha casava, a camada 1 devolvia zero transações, o arquivo
caía na camada de IA e a IA achatava a linha capturando a coluna de saldo no
lugar do valor — 29 lançamentos entraram com o saldo como valor (ver 4ac77cf).
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.domain.extrato.pdf_parser import (
    PDFParseError,
    _parse_linhas_multipagina,
    _validar_completude,
)

# Cabeçalho + 6 lançamentos. Saldo de abertura 139.235,49; fechamento 152.735,49.
_CABECALHO = [
    "Associado: SIND EXEMPLO DE COMERCIO DE PECAS E ACESSORIOS PARA VEIC E",
    "MOTOP ROL E AC NO E",
    "Cooperativa: 0752 Conta Corrente: 00031-0 Impresso em 03/08/2026 11:51:10",
    "Extrato",
    "Dados referentes ao periodo 01/07/2026 a 31/07/2026.",
    "Data Descricao Documento Valor (R$) Saldo (R$)",
    "SALDO 139.235,49",
]

_LANCAMENTOS_SALDO_POSITIVO = [
    "01/07/2026 LIQ.COBRANCA SIMPLES COB000002 13.500,00 152.735,49",
    "01/07/2026 LIQUIDACAO BOLETO 11111111000191 EXEMPLO PAGAMENTO -177,56 152.557,93",
    "01/07/2026 TED 22222222000102 EXEMPLO CORRETORA DE SEGUROS 883144 905,11 153.463,04",
    "01/07/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 153.461,85",
    "08/07/2026 APLICACAO POUPANCA -16.000,00 137.461,85",
    "31/07/2026 RECEBIMENTO PIX 33333333000103 EXEMPLO LTDA PIX_CRED 421,90 137.883,75",
]


def _negativar_saldo(linha: str) -> str:
    """Mesma linha com o saldo (último número) negativo — conta no vermelho."""
    prefixo, _, saldo = linha.rpartition(" ")
    return f"{prefixo} -{saldo}"


def _parse(linhas: list[str]):
    return _parse_linhas_multipagina(_CABECALHO + linhas, 2026)


def test_extrai_todos_os_lancamentos_do_layout_sicredi():
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)

    assert len(transacoes) == len(_LANCAMENTOS_SALDO_POSITIVO)
    assert [t.valor for t in transacoes] == [
        Decimal("13500.00"),
        Decimal("-177.56"),
        Decimal("905.11"),
        Decimal("-1.19"),
        Decimal("-16000.00"),
        Decimal("421.90"),
    ]


def test_saldo_devedor_nao_impede_a_extracao():
    """A regressão real: com saldo negativo o parser devolvia zero transações."""
    negativas = [_negativar_saldo(l) for l in _LANCAMENTOS_SALDO_POSITIVO]

    transacoes = _parse(negativas)

    assert len(transacoes) == len(_LANCAMENTOS_SALDO_POSITIVO)
    # O sinal do saldo não pode contaminar o valor do lançamento.
    assert [t.valor for t in transacoes] == [
        t.valor for t in _parse(_LANCAMENTOS_SALDO_POSITIVO)
    ]


def test_tarifa_nao_recebe_o_saldo_como_valor():
    """O lançamento exato que quebrou em produção: tarifa de R$ 1,19 a débito."""
    (tarifa,) = _parse(
        ["18/02/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 -54.881,83"]
    )

    assert tarifa.valor == Decimal("-1.19")
    assert tarifa.tipo_ofx == "DEBIT"
    assert "54.881,83" not in tarifa.historico


def test_cabecalho_nao_vira_historico_do_primeiro_lancamento():
    """Texto do preâmbulo vira `pending_desc` e vazava para o primeiro lançamento."""
    primeira, *_ = _parse(_LANCAMENTOS_SALDO_POSITIVO)

    assert primeira.historico.startswith("LIQ.COBRANCA SIMPLES")
    assert "MOTOP ROL" not in primeira.historico
    assert "Dados referentes" not in primeira.historico


def test_movimentos_reconciliam_com_o_saldo_declarado():
    """Soma dos lançamentos + saldo de abertura == saldo da última linha."""
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)

    saldo_final = Decimal("139235.49") + sum(t.valor for t in transacoes)

    assert saldo_final == Decimal("137883.75")


def test_debitos_e_creditos_seguem_o_sinal_do_valor():
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)

    for t in transacoes:
        esperado = "CREDIT" if t.valor > 0 else "DEBIT"
        assert t.tipo_ofx == esperado


# ── Saldo por lançamento ────────────────────────────────────────────────────


def test_captura_o_saldo_de_cada_lancamento():
    """A coluna de saldo é lida do extrato — antes era descartada."""
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)

    assert [t.saldo_apos for t in transacoes] == [
        Decimal("152735.49"),
        Decimal("152557.93"),
        Decimal("153463.04"),
        Decimal("153461.85"),
        Decimal("137461.85"),
        Decimal("137883.75"),
    ]


def test_saldo_devedor_e_capturado_com_o_sinal():
    """Saldo negativo é dado válido de conferência, não erro de leitura."""
    negativas = [_negativar_saldo(l) for l in _LANCAMENTOS_SALDO_POSITIVO]

    transacoes = _parse(negativas)

    assert all(t.saldo_apos is not None and t.saldo_apos < 0 for t in transacoes)


def test_saldo_nao_entra_na_identidade_de_deduplicacao():
    """O mesmo lançamento com saldo diferente precisa gerar o MESMO fitid.

    O `fitid` alimenta o `hash_dedup`. Se o saldo entrasse nele, reimportar um
    extrato corrigido criaria transações novas em vez de deduplicar contra as
    existentes — foi exatamente o que impediu a reimportação simples da
    SINCOPEÇAS, e não pode ser reintroduzido por este campo.
    """
    original = _parse(_LANCAMENTOS_SALDO_POSITIVO)
    com_outro_saldo = _parse([_negativar_saldo(l) for l in _LANCAMENTOS_SALDO_POSITIVO])

    assert [t.fitid for t in original] == [t.fitid for t in com_outro_saldo]


def test_camada_deterministica_sempre_traz_saldo():
    """`_TX_LINE` exige dois números no fim, então o saldo nunca falta aqui.

    Uma linha sem saldo simplesmente não casa com o padrão e não vira transação;
    não existe transação determinística com `saldo_apos` nulo.
    """
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)

    assert all(t.saldo_apos is not None for t in transacoes)


def test_cadeia_de_saldos_valida_o_extrato_integro():
    """O Sicredi não traz 'SALDO ANTERIOR' nem 'TOTAL DÉBITOS'.

    Antes da cadeia, `_validar_completude` rejeitava o extrato inteiro por não
    achar as linhas de resumo que procura — a correção do parser sozinha não
    tornava o Sicredi importável.
    """
    _validar_completude(_CABECALHO, _parse(_LANCAMENTOS_SALDO_POSITIVO))


def test_cadeia_rejeita_lancamento_faltando():
    """Lançamento ausente faz o saldo pular mais que o valor."""
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)
    sem_o_terceiro = transacoes[:2] + transacoes[3:]

    with pytest.raises(PDFParseError, match="não caminha"):
        _validar_completude(_CABECALHO, sem_o_terceiro)


def test_cadeia_rejeita_valor_trocado_pelo_saldo():
    """O bug real: a IA capturava a coluna de saldo no lugar do valor."""
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)
    corrompida = list(transacoes)
    corrompida[3] = replace(corrompida[3], valor=corrompida[3].saldo_apos)

    with pytest.raises(PDFParseError, match="não caminha"):
        _validar_completude(_CABECALHO, corrompida)


def test_cadeia_nao_alega_validacao_sem_saldo():
    """Sem saldo em todas as transações, a cadeia não prova nada.

    Nesse caso `_validar_completude` deve continuar exigindo as linhas de
    resumo, em vez de dar o extrato por conferido.
    """
    transacoes = _parse(_LANCAMENTOS_SALDO_POSITIVO)
    sem_saldo = [replace(t, saldo_apos=None) for t in transacoes]

    with pytest.raises(PDFParseError, match="Não foi possível validar"):
        _validar_completude(_CABECALHO, sem_saldo)


def test_origens_sem_saldo_ficam_nulas():
    """OFX e as camadas de IA constroem sem o campo — o default garante NULL.

    É o que sustenta a coluna ser nullable: ausência de saldo é estado
    permanente dessas origens, não pendência de backfill.
    """
    from src.domain.extrato.ofx_parser import TransacaoOFX

    t = TransacaoOFX(
        fitid="X",
        data=datetime(2026, 7, 1, tzinfo=UTC),
        valor=Decimal("10.00"),
        historico="PIX",
        tipo_ofx="CREDIT",
    )

    assert t.saldo_apos is None
