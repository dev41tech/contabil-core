"""Rejeições de segurança e autorização no parser fiscal."""

import pytest

from src.domain.notas.xml_parser import parse_nota_xml


def test_xml_com_dtd_e_rejeitado_antes_do_parse():
    xml = b'<!DOCTYPE NFe [<!ENTITY x "valor">]><NFe>&x;</NFe>'
    with pytest.raises(ValueError, match="DTD"):
        parse_nota_xml(xml)


def test_nfe_sem_protocolo_de_autorizacao_e_rejeitada():
    chave = "1" * 44
    xml = f"""
    <NFe xmlns="http://www.portalfiscal.inf.br/nfe">
      <infNFe Id="NFe{chave}">
        <ide><nNF>42</nNF><serie>1</serie><dhEmi>2026-01-01T10:00:00-03:00</dhEmi></ide>
        <emit><CNPJ>12345678000195</CNPJ><xNome>Emitente</xNome></emit>
        <dest><CNPJ>98765432000110</CNPJ></dest>
        <total><ICMSTot><vNF>100.00</vNF></ICMSTot></total>
      </infNFe>
    </NFe>
    """.encode()

    with pytest.raises(ValueError, match="protocolo de autorização"):
        parse_nota_xml(xml)
