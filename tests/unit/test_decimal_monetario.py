"""Regressões para impedir que valores monetários voltem a passar por float."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Numeric

from src.db.models import (
    CartaoCredito,
    Comprovante,
    CpConciliacao,
    CpDivergencia,
    CpFornecedor,
    CpLancamento,
    FaturaCartao,
    LancamentoCartao,
    NotaFiscal,
    RegistroContabil,
    Transacao,
)
from src.schemas.cartoes import LancamentoCreate
from src.schemas.comprovantes import ComprovanteCreate
from src.schemas.notas import NotaFiscalCreate


def test_colunas_monetarias_usam_numeric_com_decimal():
    colunas = [
        Transacao.__table__.c.valor,
        RegistroContabil.__table__.c.valor,
        NotaFiscal.__table__.c.valor,
        Comprovante.__table__.c.valor_documento,
        Comprovante.__table__.c.valor_pago,
        Comprovante.__table__.c.juros,
        Comprovante.__table__.c.multa,
        Comprovante.__table__.c.desconto,
        CartaoCredito.__table__.c.limite,
        FaturaCartao.__table__.c.valor_total,
        LancamentoCartao.__table__.c.valor,
        CpFornecedor.__table__.c.saldo_anterior,
        CpFornecedor.__table__.c.total_debito,
        CpFornecedor.__table__.c.total_credito,
        CpFornecedor.__table__.c.saldo_final,
        CpFornecedor.__table__.c.valor_a_pagar,
        CpLancamento.__table__.c.valor_debito,
        CpLancamento.__table__.c.valor_credito,
        CpLancamento.__table__.c.saldo_apos_lancamento,
        CpLancamento.__table__.c.valor_pago_parcial,
        CpLancamento.__table__.c.valor_saldo,
        CpConciliacao.__table__.c.valor_conciliado,
        CpDivergencia.__table__.c.valor_esperado,
        CpDivergencia.__table__.c.valor_encontrado,
        CpDivergencia.__table__.c.diferenca,
    ]

    assert all(isinstance(coluna.type, Numeric) for coluna in colunas)
    assert all(coluna.type.asdecimal is True for coluna in colunas)
    assert all(coluna.type.python_type is Decimal for coluna in colunas)


def test_schemas_preservam_decimal_desde_a_entrada():
    nota = NotaFiscalCreate(
        tipo="nfe",
        numero="1",
        cnpj_emitente="12.345.678/0001-95",
        valor="0.10",
        data_emissao=datetime(2026, 1, 1, tzinfo=UTC),
    )
    comprovante = ComprovanteCreate(valor_pago="0.20", juros="0.03")
    lancamento = LancamentoCreate(
        data_compra=datetime(2026, 1, 1, tzinfo=UTC),
        descricao="Compra",
        valor="0.30",
    )

    assert nota.valor + comprovante.valor_pago == lancamento.valor
    assert isinstance(nota.valor, Decimal)
    assert isinstance(comprovante.juros, Decimal)
    assert isinstance(lancamento.valor, Decimal)
