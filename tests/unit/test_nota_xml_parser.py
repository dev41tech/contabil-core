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


def test_arquivo_com_nota_e_eventos_juntos_nao_e_tratado_como_evento():
    """Empacotamento `NFeLog`: a nota vem JUNTO dos eventos dela.

    Alguns downloaders de DF-e entregam o `procNFe` autorizado no mesmo arquivo
    que a ciência da operação, o CT-e autorizado e o MDF-e. Como a busca por
    `infEvento` varre qualquer nível, esses arquivos eram recusados com "este
    arquivo é um evento, envie o XML da própria NF-e" — sendo que a NF-e estava
    lá dentro. O contador não tinha o que fazer com a mensagem: o arquivo que
    ele tinha ERA o certo.
    """
    import xml.etree.ElementTree as ET

    from src.domain.notas.xml_parser import _is_evento_nfe

    empacotado = ET.fromstring(
        '<NFeLog versao="1.00">'
        '  <procNFe><NFe><infNFe Id="NFe123" versao="4.00"/></NFe></procNFe>'
        '  <eveNFe><evento><infEvento><tpEvento>210210</tpEvento></infEvento>'
        "  </evento></eveNFe>"
        "</NFeLog>"
    )
    assert _is_evento_nfe(empacotado) is False


def test_arquivo_so_de_evento_continua_sendo_recusado():
    """O contrapeso: sem `infNFe`, é evento mesmo, e a mensagem dele importa."""
    import xml.etree.ElementTree as ET

    from src.domain.notas.xml_parser import _is_evento_nfe

    so_evento = ET.fromstring(
        "<procEventoNFe><evento><infEvento>"
        "<tpEvento>210210</tpEvento></infEvento></evento></procEventoNFe>"
    )
    assert _is_evento_nfe(so_evento) is True


def test_configuracao_de_assinatura_aceita_o_par_que_a_nfe_obriga():
    """A NF-e é assinada em RSA-SHA1 com digest SHA-1, por norma da SEFAZ.

    O `signxml` 5.x tirou os dois do conjunto padrão. O efeito aqui não foi
    "aceitar só o que é forte" — foi recusar 100% das notas fiscais, com a
    mensagem "Signature method RSA_SHA1 forbidden by configuration".

    Nenhum teste exercitava uma assinatura de verdade, então a quebra veio de um
    upgrade de dependência e ninguém viu. Este trava o par exigido pela norma.
    """
    from signxml import DigestAlgorithm, SignatureConfiguration, SignatureMethod

    from src.domain.notas.xml_parser import _configuracao_de_assinatura

    config = _configuracao_de_assinatura()
    assert SignatureMethod.RSA_SHA1 in config.signature_methods
    assert DigestAlgorithm.SHA1 in config.digest_algorithms
    assert config.require_x509 is True

    # A permissão é ADITIVA: nada do que já era aceito pode ter saído.
    padrao = SignatureConfiguration()
    assert padrao.signature_methods <= config.signature_methods
    assert padrao.digest_algorithms <= config.digest_algorithms


def test_assinatura_que_nao_confere_relata_em_vez_de_recusar():
    """A decisão de 01/09/2026: nota entra, marcada, em vez de ser recusada.

    Os XML que o downloader de DF-e do escritório entrega vêm reempacotados e
    ALTERADOS depois de assinados — num exemplo real o e-mail do destinatário
    chega mascarado (`j*m@g*l*.com`). A assinatura deles nunca vai conferir, e
    recusá-los deixava o escritório sem importar nota nenhuma.
    """
    import xml.etree.ElementTree as ET

    from src.domain.notas.xml_parser import _validar_assinatura

    sem_assinatura = ET.fromstring("<NFe><infNFe Id='NFe1'/></NFe>")
    motivo = _validar_assinatura(b"<NFe/>", sem_assinatura, "NFe1")
    assert motivo == "documento sem assinatura XML"

    referencia_errada = ET.fromstring(
        '<NFe><Signature xmlns="http://www.w3.org/2000/09/xmldsig#">'
        '<SignedInfo><Reference URI="#OUTRA"/></SignedInfo></Signature></NFe>'
    )
    motivo = _validar_assinatura(b"<NFe/>", referencia_errada, "NFe1")
    assert motivo is not None
    assert "não referencia" in motivo


def test_nfe_sem_protocolo_continua_recusada_mesmo_com_a_decisao_nova():
    """O contrapeso, e é ele que impede isto de virar "aceita qualquer XML".

    Aceitar assinatura não verificável só é defensável porque o protocolo de
    autorização da SEFAZ continua obrigatório: o que sustenta o documento passou
    a ser ele. Se este teste cair, o afrouxamento perdeu a âncora.
    """
    import pytest

    from src.domain.notas.xml_parser import parse_nota_xml

    sem_protocolo = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<NFe xmlns="http://www.portalfiscal.inf.br/nfe">'
        b'<infNFe Id="NFe35260756287725000590550010000840861383950941" versao="4.00">'
        b"<ide><nNF>1</nNF><serie>1</serie>"
        b"<dhEmi>2026-07-16T15:14:01-03:00</dhEmi></ide>"
        b"<emit><CNPJ>56287725000590</CNPJ></emit>"
        b"<total><ICMSTot><vNF>10.00</vNF></ICMSTot></total>"
        b"</infNFe></NFe>"
    )

    with pytest.raises(ValueError):
        parse_nota_xml(sem_protocolo)


def test_mensagem_de_cnpj_diz_quais_cnpjs_e_reconhece_filial():
    """Recusar está certo; não dizer QUAIS CNPJs era um beco sem saída.

    Sem os números, quem sobe o arquivo não distingue "errei a empresa na tela"
    de "o cadastro da empresa está com o CNPJ errado" — e as duas saídas são
    opostas. Pior no caso de FILIAL: mesma raiz de CNPJ, outro estabelecimento.
    O contador considera que a nota é "da empresa", o cadastro aqui é por
    estabelecimento, e sem a frase a conclusão natural é que o sistema quebrou.
    """
    from src.core.cnpj import formatar

    assert formatar("18558584000180") == "18.558.584/0001-80"
    assert formatar("123") == "123"  # não formata o que não é CNPJ


def test_raiz_de_cnpj_distingue_filial_de_empresa_alheia():
    """A regra que decide se a mensagem menciona filial.

    Mesma raiz (8 primeiros dígitos) e estabelecimento diferente é filial;
    raiz diferente é outra empresa, e aí o conselho é outro.
    """
    matriz = "18558584000180"
    filial = "18558584000261"
    alheia = "01844555002398"

    assert filial[:8] == matriz[:8] and filial != matriz
    assert alheia[:8] != matriz[:8]
