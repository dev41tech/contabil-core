"""Validação de completude da extração de extratos em PDF."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.domain.extrato.ofx_parser import TransacaoOFX
from src.domain.extrato.pdf_parser import PDFParseError, _validar_completude


def _transacao(valor: str) -> TransacaoOFX:
    return TransacaoOFX(
        fitid="PDFTESTE",
        data=datetime(2026, 1, 1, tzinfo=UTC),
        valor=Decimal(valor),
        historico="Teste",
        tipo_ofx="CREDIT" if Decimal(valor) > 0 else "DEBIT",
    )


def test_completude_reconcilia_saldos_declarados():
    linhas = ["SALDO ANTERIOR 1.000,00 C", "SALDO FINAL 1.250,00 C"]
    _validar_completude(linhas, [_transacao("250.00")])


def test_completude_rejeita_extracao_parcial():
    linhas = ["SALDO ANTERIOR 1.000,00 C", "SALDO FINAL 1.250,00 C"]
    with pytest.raises(PDFParseError, match="não reconciliam"):
        _validar_completude(linhas, [_transacao("100.00")])


def test_completude_rejeita_quando_documento_nao_declara_totais():
    with pytest.raises(PDFParseError, match="Não foi possível validar"):
        _validar_completude(["01/01 PIX 100,00"], [_transacao("100.00")])
