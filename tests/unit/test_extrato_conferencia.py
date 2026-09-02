"""Conferência de completude pelo saldo de fechamento declarado no arquivo.

Os números destes testes são os dos seis extratos Sicoob de jan–jun/2026 da
conta que motivou a mudança — a cadeia real, com os valores preservados porque
é o encadeamento que interessa provar, não a identidade de quem movimentou.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.domain.extrato.conferencia import conferir_fechamento


def test_cadeia_que_fecha_nao_alerta():
    """Fevereiro: 911,18 de janeiro − 94,94 movimentados = 816,24 declarado."""
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("911.18"),
        data_anterior=date(2026, 1, 30),
        movimento=Decimal("-94.94"),
        saldo_declarado=Decimal("816.24"),
        data_declarada=date(2026, 2, 27),
    )

    assert alerta is None


def test_periodo_faltando_e_a_diferenca_do_periodo_inteiro():
    """Fevereiro e depois abril: a conta não fecha por março inteiro (246,08).

    É o caso legítimo mais comum, e a razão de isto ser aviso e não recusa —
    abril está correto, o que falta é março.
    """
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("816.24"),
        data_anterior=date(2026, 2, 27),
        movimento=Decimal("1621.61"),
        saldo_declarado=Decimal("2191.77"),
        data_declarada=date(2026, 4, 30),
    )

    assert alerta is not None
    assert "R$ 246,08" in alerta
    # As duas pontas precisam aparecer: é o que torna o buraco visível em vez de
    # deixar um número sem explicação.
    assert "27/02/2026" in alerta
    assert "30/04/2026" in alerta


def test_alerta_diz_o_declarado_e_o_esperado():
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("100.00"),
        data_anterior=date(2026, 1, 31),
        movimento=Decimal("50.00"),
        saldo_declarado=Decimal("160.00"),
        data_declarada=date(2026, 2, 28),
    )

    assert "R$ 160,00" in alerta   # o que o arquivo declara
    assert "R$ 150,00" in alerta   # o que a conta anterior esperava
    assert "R$ 10,00" in alerta    # a diferença


def test_primeiro_arquivo_da_conta_nao_alega_conferencia():
    """Sem âncora anterior não há o que conferir — e silêncio aqui é
    "não verificado", não "verificado e certo"."""
    alerta = conferir_fechamento(
        saldo_anterior=None,
        data_anterior=None,
        movimento=Decimal("-6968.80"),
        saldo_declarado=Decimal("911.18"),
        data_declarada=date(2026, 1, 30),
    )

    assert alerta is None


def test_arquivo_sem_saldo_declarado_nao_alega_conferencia():
    """OFX de banco que não emite LEDGERBAL, e todo PDF, caem aqui."""
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("911.18"),
        data_anterior=date(2026, 1, 30),
        movimento=Decimal("-94.94"),
        saldo_declarado=None,
        data_declarada=None,
    )

    assert alerta is None


def test_conta_no_vermelho_fecha_igual():
    """Saldo negativo é situação normal, e já custou um bug neste módulo.

    O parser de PDF recusava conta no vermelho porque o grupo do saldo não
    aceitava sinal; a conferência não pode repetir a classe do erro.
    """
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("-7082.79"),
        data_anterior=date(2026, 3, 31),
        movimento=Decimal("-1000.00"),
        saldo_declarado=Decimal("-8082.79"),
        data_declarada=date(2026, 4, 30),
    )

    assert alerta is None


def test_diferenca_de_um_centavo_nao_passa():
    """A conferência é exata: erro de arredondamento aqui é erro de leitura."""
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("911.18"),
        data_anterior=date(2026, 1, 30),
        movimento=Decimal("-94.94"),
        saldo_declarado=Decimal("816.25"),
        data_declarada=date(2026, 2, 27),
    )

    assert alerta is not None
    assert "R$ 0,01" in alerta


def test_alerta_cabe_na_coluna():
    """`extrato_importacoes.alerta_saldo` é VARCHAR(500)."""
    alerta = conferir_fechamento(
        saldo_anterior=Decimal("-99999999.99"),
        data_anterior=date(2026, 1, 30),
        movimento=Decimal("-99999999.99"),
        saldo_declarado=Decimal("99999999.99"),
        data_declarada=date(2026, 2, 27),
    )

    assert alerta is not None
    assert len(alerta) <= 500
