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


def test_evento_de_nfe_e_rejeitado_com_mensagem_especifica():
    """Um XML de evento (Carta de Correção, Cancelamento, Manifestação do
    Destinatário/Ciência da Operação) usa o mesmo namespace da NF-e — sem essa
    checagem específica, cai em `_parse_nfe` e falha com "infNFe não
    encontrado", sem dizer ao usuário qual é o arquivo certo a enviar. Visto
    num teste real de QA: `tpEvento` 210210 = Ciência da Operação."""
    xml = b"""
    <procEventoNFe versao="1.00" xmlns="http://www.portalfiscal.inf.br/nfe">
      <evento versao="1.00">
        <infEvento Id="ID21021041260702821150000191550010002284121003588757011">
          <cOrgao>91</cOrgao>
          <chNFe>41260702821150000191550010002284121003588757</chNFe>
          <tpEvento>210210</tpEvento>
          <detEvento versao="1.00"><descEvento>Ciencia da Operacao</descEvento></detEvento>
        </infEvento>
      </evento>
    </procEventoNFe>
    """

    with pytest.raises(ValueError, match="evento da NF-e"):
        parse_nota_xml(xml)


def test_evento_de_nfe_sem_tipo_reconhecido_ainda_da_mensagem_generica_de_evento():
    xml = b"""
    <procEventoNFe versao="1.00" xmlns="http://www.portalfiscal.inf.br/nfe">
      <evento versao="1.00">
        <infEvento Id="ID1"><tpEvento>999999</tpEvento></infEvento>
      </evento>
    </procEventoNFe>
    """
    with pytest.raises(ValueError, match="evento da NF-e \\(evento\\)"):
        parse_nota_xml(xml)
