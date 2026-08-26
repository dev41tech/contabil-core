"""Testes unitários — barreira contra valor de transação não confiável.

Os casos vêm de dados reais de produção (SINCOPEÇAS, agosto/2026), onde um
extrato em PDF caiu na camada de IA do parser e 29 transações foram gravadas
com o saldo da conta no lugar do valor — uma tarifa de R$ 1,19 virou
R$ 54.881,83 a crédito. Nenhuma chegou a virar lançamento contábil, mas só
porque ninguém as classificou antes de o problema aparecer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.db.models import Transacao
from src.domain.extrato.validacao import (
    historico_parece_linha_crua,
    motivo_valor_nao_confiavel,
    valores_na_linha,
)
from src.domain.neo.engine import motivo_para_nao_contabilizar

# Linhas exatamente como estavam no banco de produção.
_TARIFA = "18/02/2026 TARIFA COM R LIQUIDACAO COB000001 -1,19 -54.881,83"
_CONVENIO = "20/02/2026 DEBITO CONVENIOS 76417005000186 PMCURIT-G -1.788,13 -70.254,20"
_LIMPA = "LIQ.COBRANCA SIMPLES COB000001"


def test_rejeita_quando_o_valor_e_o_saldo():
    """O caso concreto: o último número da linha é o saldo, não a transação."""
    motivo = motivo_valor_nao_confiavel(_TARIFA, Decimal("54881.83"))

    assert motivo is not None
    assert "saldo da conta" in motivo
    # A mensagem precisa dizer qual era o valor certo — é o que permite ao
    # contador lançar à mão sem reabrir o PDF.
    assert "R$ 1,19" in motivo


def test_aceita_quando_o_valor_e_o_da_transacao():
    """Mesma linha crua, valor correto: a descrição é feia, o número presta.

    Rejeitar aqui abriria buraco no extrato sem necessidade — e buraco também
    quebra conciliação.
    """
    assert motivo_valor_nao_confiavel(_TARIFA, Decimal("1.19")) is None


def test_rejeita_quando_o_valor_nao_bate_com_nada_na_linha():
    """Valor que não corresponde a nenhum número da linha veio de lugar
    nenhum; sem conseguir amarrar à origem, não dá para confiar."""
    motivo = motivo_valor_nao_confiavel(_CONVENIO, Decimal("999.00"))

    assert motivo is not None
    assert "não corresponde a nenhum valor" in motivo


@pytest.mark.parametrize(
    ("historico", "valor"),
    [
        (_LIMPA, Decimal("5846.40")),
        ("PIX ENVIADO MARIA SILVA", Decimal("350.00")),
        ("TARIFA BANCARIA", Decimal("1.19")),
        ("PGTO FORNECEDOR 1.234,56", Decimal("9999.99")),
    ],
)
def test_descricao_limpa_nunca_e_rejeitada(historico: str, valor: Decimal):
    """Sem dois valores na linha não há o que comparar, e o parser fez o
    trabalho dele. Um único número na descrição também não basta: não existe
    par valor/saldo para contrastar.
    """
    assert motivo_valor_nao_confiavel(historico, valor) is None


def test_sinal_do_valor_nao_atrapalha_a_comparacao():
    """A linha traz o valor negativo e o banco grava o módulo — a comparação é
    em módulo dos dois lados, senão o débito legítimo seria rejeitado."""
    assert motivo_valor_nao_confiavel(_TARIFA, Decimal("-1.19")) is None


def test_valores_na_linha_le_o_formato_brasileiro():
    assert valores_na_linha(_TARIFA) == [Decimal("1.19"), Decimal("54881.83")]
    assert valores_na_linha(_LIMPA) == []


def test_linha_crua_e_sinalizada_mesmo_quando_o_valor_confere():
    """Serve de aviso, não de rejeição: indica que aquele arquivo passou pelo
    caminho heurístico e merece conferência."""
    assert historico_parece_linha_crua(_TARIFA) is True
    assert historico_parece_linha_crua(_LIMPA) is False


@pytest.mark.parametrize("historico", [_TARIFA, _CONVENIO])
def test_motor_usa_a_mesma_regua_da_importacao(historico: str):
    """O NEO não pode ter uma segunda definição de "valor confiável".

    Se as duas divergirem, a fila barra um conjunto e a importação recusa
    outro — e a transação que escapar das duas entra no razão com valor
    errado. A mensagem do motor acrescenta o que fazer, porque quem a lê está
    na tela de classificação, não na de importação.
    """
    valor = valores_na_linha(historico)[-1]
    transacao = Transacao(historico=historico, valor=valor, dc="C")

    assert motivo_valor_nao_confiavel(historico, valor) is not None
    motivo = motivo_para_nao_contabilizar(transacao)
    assert motivo is not None
    assert "saldo" in motivo
    assert "Corrija a importação" in motivo


def test_transacao_com_valor_confiavel_passa_pelo_motor():
    transacao = Transacao(historico=_LIMPA, valor=Decimal("1.19"), dc="D")
    assert motivo_para_nao_contabilizar(transacao) is None
