"""Testes unitários — OFX parser."""

from __future__ import annotations

from datetime import timezone

import pytest

from src.domain.extrato.ofx_parser import OFXParseError, TransacaoOFX, parse_ofx

_OFX1_COMPLETO = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240115120000
<TRNAMT>1500.00
<FITID>TX001
<MEMO>TED RECEBIDA CLIENTE
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240116
<TRNAMT>-250.50
<FITID>TX002
<NAME>BOLETO PAGO
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

_OFX2_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<OFX>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <STMTRS>
        <BANKTRANLIST>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20240201</DTPOSTED>
            <TRNAMT>999.99</TRNAMT>
            <FITID>XML001</FITID>
            <MEMO>DEPOSITO PIX</MEMO>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""

_OFX_COM_TIMEZONE = """\
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20240301120000[-3:BRT]
<TRNAMT>-100.00
<FITID>TZ001
<MEMO>TESTE TIMEZONE
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_parse_ofx1_dois_registros():
    transacoes = parse_ofx(_OFX1_COMPLETO)
    assert len(transacoes) == 2


def test_parse_ofx1_credito():
    transacoes = parse_ofx(_OFX1_COMPLETO)
    cred = next(t for t in transacoes if t.fitid == "TX001")
    assert cred.valor == 1500.00
    assert cred.historico == "TED RECEBIDA CLIENTE"
    assert cred.tipo_ofx == "CREDIT"
    assert cred.data.tzinfo == timezone.utc


def test_parse_ofx1_debito():
    transacoes = parse_ofx(_OFX1_COMPLETO)
    deb = next(t for t in transacoes if t.fitid == "TX002")
    assert deb.valor == -250.50
    assert deb.historico == "BOLETO PAGO"
    assert deb.tipo_ofx == "DEBIT"


def test_parse_ofx1_data_com_hora():
    transacoes = parse_ofx(_OFX1_COMPLETO)
    cred = next(t for t in transacoes if t.fitid == "TX001")
    assert cred.data.year == 2024
    assert cred.data.month == 1
    assert cred.data.day == 15


def test_parse_ofx2_xml():
    transacoes = parse_ofx(_OFX2_XML)
    assert len(transacoes) == 1
    t = transacoes[0]
    assert t.fitid == "XML001"
    assert t.valor == 999.99
    assert t.historico == "DEPOSITO PIX"


def test_parse_ofx1_timezone_removido():
    """Tags de timezone OFX devem ser ignoradas."""
    transacoes = parse_ofx(_OFX_COM_TIMEZONE)
    assert len(transacoes) == 1
    assert transacoes[0].data.year == 2024
    assert transacoes[0].data.month == 3


def test_parse_ofx_vazio_retorna_lista_vazia():
    # OFX sem transações deve retornar lista vazia, não lançar
    ofx_sem_tx = "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST></BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    result = parse_ofx(ofx_sem_tx)
    assert result == []


def test_parse_ofx_com_bom():
    """Arquivo com BOM UTF-8 deve ser processado normalmente."""
    com_bom = "\ufeff" + _OFX1_COMPLETO
    transacoes = parse_ofx(com_bom)
    assert len(transacoes) == 2


def test_parse_ofx_valor_com_virgula():
    ofx = """\
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240101
<TRNAMT>1.500,00
<FITID>V001
<MEMO>VALOR COM VIRGULA
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""
    # Valor com vírgula deve ser convertido (vírgula → ponto)
    transacoes = parse_ofx(ofx)
    assert len(transacoes) == 1
    assert transacoes[0].valor == 1500.00


def test_parse_ofx_registro_sem_fitid_ignorado():
    """Bloco sem FITID deve ser silenciosamente ignorado."""
    ofx = """\
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240101
<TRNAMT>100.00
<MEMO>SEM FITID
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20240101
<TRNAMT>200.00
<FITID>COM_FITID
<MEMO>COM FITID
</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""
    transacoes = parse_ofx(ofx)
    assert len(transacoes) == 1
    assert transacoes[0].fitid == "COM_FITID"
