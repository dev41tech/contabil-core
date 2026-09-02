"""Testes unitários — OFX parser."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from src.domain.extrato.ofx_parser import parse_ofx, parse_ofx_detalhado

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
    assert cred.valor == Decimal("1500.00")
    assert cred.historico == "TED RECEBIDA CLIENTE"
    assert cred.tipo_ofx == "CREDIT"
    # Data de calendário, sem hora nem fuso: quem lê não reinterpreta o dia.
    assert cred.data == date(2024, 1, 15)


def test_parse_ofx1_debito():
    transacoes = parse_ofx(_OFX1_COMPLETO)
    deb = next(t for t in transacoes if t.fitid == "TX002")
    assert deb.valor == Decimal("-250.50")
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
    assert t.valor == Decimal("999.99")
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
    assert transacoes[0].valor == Decimal("1500.00")


def test_parse_ofx_registro_sem_fitid_reportado():
    """Bloco sem FITID é rejeitado, mas nunca desaparece da contagem."""
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
    resultado = parse_ofx_detalhado(ofx)
    assert resultado.total_blocos == 2
    assert len(resultado.erros) == 1
    assert "FITID" in resultado.erros[0]
    assert resultado.transacoes[0].fitid == "COM_FITID"


def test_parse_ofx_data_com_fracao_e_timezone():
    """Hora, fração e offset são aceitos, mas o que sobra é a data de calendário."""
    ofx = _OFX_COM_TIMEZONE.replace(
        "20240301120000[-3:BRT]", "20240301120000.125[-3:BRT]"
    )

    transacao = parse_ofx(ofx)[0]

    assert transacao.data == date(2024, 3, 1)


def test_lancamento_noturno_fica_no_dia_do_banco():
    """22h de 01/03 em Brasília é 01/03, embora seja 02/03 em UTC.

    Enquanto a data era instante, converter para UTC empurrava o lançamento para
    o dia seguinte. A data de negócio é a do fuso que o próprio OFX declara.
    """
    ofx = _OFX_COM_TIMEZONE.replace(
        "20240301120000[-3:BRT]", "20240301220000[-3:BRT]"
    )

    transacao = parse_ofx(ofx)[0]

    assert transacao.data == date(2024, 3, 1)


# ─────────────────────────────────────────── MEMO + NAME: os dois campos de descrição

# Estrutura real do OFX do Sicoob (FID 756), com razão social, CNPJ e valores
# fictícios. O arquivo do cliente nunca entra no repositório — o que importa
# reproduzir aqui é que MEMO traz o TIPO da operação e NAME traz a contraparte.
_OFX_SICOOB = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT</TRNTYPE>
<DTPOSTED>20260130120000[-3:BRT]</DTPOSTED>
<TRNAMT>-600.00</TRNAMT>
<FITID>20260130600001</FITID>
<CHECKNUM>0</CHECKNUM>
<REFNUM>Pix</REFNUM>
<MEMO>PIX EMITIDO OUTRA IF</MEMO>
<NAME>Pagamento Pix 11.222.333 0001-44</NAME>
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT</TRNTYPE>
<DTPOSTED>20260129120000[-3:BRT]</DTPOSTED>
<TRNAMT>1200.00</TRNAMT>
<FITID>20260129120001</FITID>
<MEMO>CRED.TED-STR</MEMO>
<NAME>METALURGICA EXEMPLO LTDA</NAME>
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT</TRNTYPE>
<DTPOSTED>20260128120000[-3:BRT]</DTPOSTED>
<TRNAMT>-19.90</TRNAMT>
<FITID>20260128019901</FITID>
<MEMO>TARIFA COBRANCA</MEMO>
<NAME></NAME>
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>911.18</BALAMT>
<DTASOF>20260130120000[-3:BRT]</DTASOF>
</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def test_historico_junta_memo_e_name():
    """No Sicoob o MEMO é o tipo da operação e o NAME é a contraparte.

    Enquanto a regra era ``MEMO or NAME``, o NAME nunca era lido — todo banco
    que preenche um preenche o outro — e 151 PIX de um mesmo mês chegavam à tela
    com o texto idêntico ``PIX EMITIDO OUTRA IF``.
    """
    transacoes = parse_ofx(_OFX_SICOOB)

    pix = next(t for t in transacoes if t.fitid == "20260130600001")
    assert pix.historico == "PIX EMITIDO OUTRA IF Pagamento Pix 11.222.333 0001-44"


def test_historico_preserva_o_documento_da_contraparte():
    """O CNPJ só existe no NAME, e é por ele que o NEO resolve contraparte."""
    pix = next(t for t in parse_ofx(_OFX_SICOOB) if t.fitid == "20260130600001")

    assert "11.222.333 0001-44" in pix.historico


def test_historico_sem_name_fica_so_com_o_memo():
    tarifa = next(t for t in parse_ofx(_OFX_SICOOB) if t.fitid == "20260128019901")

    assert tarifa.historico == "TARIFA COBRANCA"


def test_historico_nao_repete_texto_presente_nos_dois_campos():
    """Banco que duplica a informação não deve produzir histórico duplicado."""
    ofx = _OFX_SICOOB.replace(
        "<NAME>METALURGICA EXEMPLO LTDA</NAME>",
        "<NAME>CRED.TED-STR</NAME>",
    )

    ted = next(t for t in parse_ofx(ofx) if t.fitid == "20260129120001")

    assert ted.historico == "CRED.TED-STR"


def test_historico_colapsa_espacos_do_preenchimento_do_banco():
    """O Sicoob preenche o MEMO com espaços à direita até o tamanho do campo."""
    ofx = _OFX_SICOOB.replace(
        "<MEMO>TARIFA COBRANCA</MEMO>",
        "<MEMO>DEB.IOF                          </MEMO>",
    )

    iof = next(t for t in parse_ofx(ofx) if t.fitid == "20260128019901")

    assert iof.historico == "DEB.IOF"


def test_regra_cadastrada_pelo_memo_continua_alcancando_o_historico_novo():
    """A razão de o MEMO vir PRIMEIRO, e o motivo de isto não quebrar o escritório.

    O casamento por substring procura o texto da regra DENTRO do histórico. Com
    o MEMO na frente, toda regra já cadastrada pelo texto dele segue casando
    depois da mudança — se este teste cair, a mudança de ordem quebrou as regras
    do escritório em silêncio, e o sintoma seria lançamento voltando a "sem
    regra" sem nada indicando por quê.
    """
    from src.core.texto import normalizar_para_match

    pix = next(t for t in parse_ofx(_OFX_SICOOB) if t.fitid == "20260130600001")

    regra_existente = normalizar_para_match("PIX EMITIDO OUTRA IF")
    assert regra_existente in normalizar_para_match(pix.historico)


# ─────────────────────────────────────────────── LEDGERBAL: o saldo do período

def test_ledgerbal_traz_saldo_e_data_do_fechamento():
    """O OFX não traz saldo por lançamento, mas traz o do período — e ninguém lia."""
    resultado = parse_ofx_detalhado(_OFX_SICOOB)

    assert resultado.saldo_declarado == Decimal("911.18")
    assert resultado.data_saldo == date(2026, 1, 30)


def test_saldo_por_lancamento_continua_nulo_no_ofx():
    """`LEDGERBAL` é do PERÍODO. Espalhá-lo por lançamento seria inventar dado."""
    assert all(t.saldo_apos is None for t in parse_ofx(_OFX_SICOOB))


def test_sem_ledgerbal_nao_alega_saldo():
    ofx = re.sub(r"<LEDGERBAL>.*?</LEDGERBAL>", "", _OFX_SICOOB, flags=re.DOTALL)

    resultado = parse_ofx_detalhado(ofx)

    assert resultado.saldo_declarado is None
    assert resultado.data_saldo is None


def test_availbal_nao_e_confundido_com_ledgerbal():
    """Saldo disponível desconta limite e bloqueio: não fecha com os lançamentos.

    Lê-lo como fechamento faria a conferência acusar diferença em arquivo certo.
    """
    ofx = _OFX_SICOOB.replace(
        "</LEDGERBAL>",
        "</LEDGERBAL>\n<AVAILBAL>\n<BALAMT>5000.00</BALAMT>\n"
        "<DTASOF>20260130120000[-3:BRT]</DTASOF>\n</AVAILBAL>",
    )

    assert parse_ofx_detalhado(ofx).saldo_declarado == Decimal("911.18")


def test_dois_ledgerbal_no_mesmo_arquivo_nao_elegem_nenhum():
    """Âncora escolhida no chute é pior que âncora nenhuma."""
    ofx = _OFX_SICOOB.replace(
        "</STMTRS>",
        "<LEDGERBAL>\n<BALAMT>77.00</BALAMT>\n"
        "<DTASOF>20260130120000[-3:BRT]</DTASOF>\n</LEDGERBAL>\n</STMTRS>",
    )

    assert parse_ofx_detalhado(ofx).saldo_declarado is None


def test_ledgerbal_em_formato_brasileiro():
    ofx = _OFX_SICOOB.replace("<BALAMT>911.18</BALAMT>", "<BALAMT>1.911,18</BALAMT>")

    assert parse_ofx_detalhado(ofx).saldo_declarado == Decimal("1911.18")
