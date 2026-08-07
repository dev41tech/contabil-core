"""Parser de XML para NF-e 4.0 e NFS-e ABRASF.

Suporta:
- NF-e 4.0 (namespace http://www.portalfiscal.inf.br/nfe)
- NFS-e ABRASF (sem namespace fixo)
- Arquivos com e sem namespace
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

NFE_NS = "http://www.portalfiscal.inf.br/nfe"


@dataclass
class NotaParseada:
    tipo: str  # "nfe" ou "nfse"
    numero: str
    serie: Optional[str]
    cnpj_emitente: str
    nome_emitente: Optional[str]
    cnpj_destinatario: Optional[str]
    valor: Decimal
    data_emissao: datetime
    chave_acesso: Optional[str]
    observacao: Optional[str]


def _safe_text(element: Optional[ET.Element]) -> Optional[str]:
    """Retorna o texto do elemento ou None, sem lançar exceção."""
    if element is None:
        return None
    text = element.text
    if text is None:
        return None
    return text.strip() or None


def _find(parent: ET.Element, *tags: str, ns: Optional[str] = None) -> Optional[ET.Element]:
    """Tenta localizar um elemento por caminho de tags.

    Primeiro tenta com namespace (se fornecido), depois sem namespace.
    Suporta busca recursiva simples quando a tag inicia com '//'.
    """
    def _search_with_prefix(root: ET.Element, path_tags: tuple[str, ...], prefix: str) -> Optional[ET.Element]:
        current = root
        for tag in path_tags:
            if tag.startswith("//"):
                # Busca recursiva
                real_tag = tag[2:]
                found = _find_recursive(current, real_tag, prefix)
                if found is None:
                    return None
                current = found
            else:
                qualified = f"{{{prefix}}}{tag}" if prefix else tag
                child = current.find(qualified)
                if child is None:
                    return None
                current = child
        return current

    def _find_recursive(root: ET.Element, tag: str, prefix: str) -> Optional[ET.Element]:
        qualified = f"{{{prefix}}}{tag}" if prefix else tag
        # Tenta no próprio elemento
        found = root.find(f".//{qualified}")
        return found

    # Tenta com namespace
    if ns:
        result = _search_with_prefix(parent, tags, ns)
        if result is not None:
            return result

    # Tenta sem namespace
    result = _search_with_prefix(parent, tags, "")
    return result


def _normalizar_cnpj(valor: Optional[str]) -> Optional[str]:
    """Remove formatação e reaplica máscara XX.XXX.XXX/XXXX-XX."""
    if valor is None:
        return None
    digits = re.sub(r"\D", "", valor)
    if len(digits) != 14:
        return None
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def _parse_datetime(valor: Optional[str]) -> Optional[datetime]:
    """Tenta parsear data/datetime em formatos comuns de NF-e e NFS-e."""
    if not valor:
        return None
    valor = valor.strip()
    # Formatos possíveis
    formatos = [
        "%Y-%m-%dT%H:%M:%S%z",   # ISO com timezone: 2026-01-15T10:00:00-03:00
        "%Y-%m-%dT%H:%M:%S",     # ISO sem timezone
        "%Y-%m-%d",              # Só data
    ]
    for fmt in formatos:
        try:
            return datetime.strptime(valor, fmt)
        except ValueError:
            continue
    return None


def _parse_decimal(valor: Optional[str], campo: str) -> Decimal:
    try:
        parsed = Decimal(valor or "")
    except InvalidOperation as exc:
        raise ValueError(f"Campo {campo} ausente ou inválido.") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Campo {campo} deve ser um valor positivo.")
    return parsed


def _validar_assinatura(conteudo: bytes, root: ET.Element, referencia: str) -> None:
    """Valida matematicamente a assinatura XML usando o certificado embutido."""
    signature = _find_by_local_tag(root, "Signature")
    if signature is None:
        raise ValueError("Documento fiscal sem assinatura XML.")

    reference = _find_by_local_tag(signature, "Reference")
    if reference is None or reference.get("URI") != f"#{referencia}":
        raise ValueError("Assinatura XML não referencia a identificação do documento fiscal.")

    cert_elem = _find_by_local_tag(signature, "X509Certificate")
    cert_text = _safe_text(cert_elem)
    if not cert_text:
        raise ValueError("Assinatura XML sem certificado X.509.")

    try:
        # O certificado explícito impede que o verificador confie em uma chave
        # indicada fora do próprio documento e valida digest + SignedInfo.
        from cryptography import x509
        from signxml import SignatureConfiguration, XMLVerifier

        cert_b64 = re.sub(r"\s+", "", cert_text)
        cert_der = b64decode(cert_b64, validate=True)
        cert_pem = (
            "-----BEGIN CERTIFICATE-----\n"
            + "\n".join(
                cert_b64[i:i + 64]
                for i in range(0, len(cert_b64), 64)
            )
            + "\n-----END CERTIFICATE-----\n"
        )

        # Certificados expirados hoje ainda podem ter assinado validamente uma
        # nota histórica; o protocolo de autorização fornece a temporalidade.
        x509.load_der_x509_certificate(cert_der)
        XMLVerifier().verify(
            conteudo,
            x509_cert=cert_pem,
            expect_config=SignatureConfiguration(require_x509=True),
        )
    except ValueError:
        raise
    except ImportError as exc:
        raise ValueError("Validação criptográfica de assinatura indisponível no servidor.") from exc
    except Exception as exc:
        raise ValueError("Assinatura XML inválida.") from exc


def _detect_namespace(root: ET.Element) -> Optional[str]:
    """Detecta o namespace do elemento raiz."""
    tag = root.tag
    if tag.startswith("{"):
        end = tag.index("}")
        return tag[1:end]
    return None


def _strip_ns(tag: str) -> str:
    """Remove o namespace de uma tag."""
    if tag.startswith("{"):
        return tag[tag.index("}") + 1:]
    return tag


def _is_nfe(root: ET.Element) -> bool:
    """Verifica se o XML é uma NF-e."""
    ns = _detect_namespace(root)
    root_tag = _strip_ns(root.tag)

    if root_tag in ("nfeProc", "NFe"):
        return True
    if ns == NFE_NS:
        return True

    # Tenta encontrar infNFe em qualquer nível
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "infNFe":
            return True

    return False


def _is_nfse(root: ET.Element) -> bool:
    """Verifica se o XML é uma NFS-e ABRASF."""
    root_tag = _strip_ns(root.tag)
    if "Nfse" in root_tag or "CompNfse" in root_tag:
        return True

    # Verifica tags internas características
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag in ("CompNfse", "InfNfse"):
            return True

    return False


def _find_by_local_tag(root: ET.Element, local_tag: str) -> Optional[ET.Element]:
    """Busca recursiva por tag ignorando namespace."""
    for elem in root.iter():
        if _strip_ns(elem.tag) == local_tag:
            return elem
    return None


def _find_path_local(root: ET.Element, *local_tags: str) -> Optional[ET.Element]:
    """Percorre um caminho de tags ignorando namespaces."""
    current = root
    for tag in local_tags:
        found = None
        # Primeiro tenta filhos diretos
        for child in current:
            if _strip_ns(child.tag) == tag:
                found = child
                break
        if found is None:
            return None
        current = found
    return current


def _parse_nfe(root: ET.Element) -> NotaParseada:
    """Extrai dados de uma NF-e 4.0."""
    # Localiza infNFe em qualquer nível
    inf_nfe = _find_by_local_tag(root, "infNFe")
    if inf_nfe is None:
        raise ValueError("Elemento infNFe não encontrado no XML NF-e.")

    # chave_acesso via atributo Id
    id_attr = inf_nfe.get("Id", "")
    chave_acesso = id_attr[3:] if id_attr.startswith("NFe") else None
    if chave_acesso == "":
        chave_acesso = None

    # ide
    ide = _find_path_local(inf_nfe, "ide")
    numero = _safe_text(_find_path_local(ide, "nNF")) if ide is not None else None
    serie = _safe_text(_find_path_local(ide, "serie")) if ide is not None else None
    dh_emi = _safe_text(_find_path_local(ide, "dhEmi")) if ide is not None else None
    data_emissao = _parse_datetime(dh_emi)

    # emit
    emit = _find_path_local(inf_nfe, "emit")
    cnpj_emitente_raw = _safe_text(_find_path_local(emit, "CNPJ")) if emit is not None else None
    nome_emitente = _safe_text(_find_path_local(emit, "xNome")) if emit is not None else None
    cnpj_emitente = _normalizar_cnpj(cnpj_emitente_raw)

    # dest
    dest = _find_path_local(inf_nfe, "dest")
    cnpj_destinatario_raw = None
    nome_destinatario = None
    if dest is not None:
        cnpj_dest_elem = _find_path_local(dest, "CNPJ")
        cpf_dest_elem = _find_path_local(dest, "CPF")
        if cnpj_dest_elem is not None:
            cnpj_destinatario_raw = _safe_text(cnpj_dest_elem)
        elif cpf_dest_elem is not None:
            cnpj_destinatario_raw = _safe_text(cpf_dest_elem)
        nome_destinatario = _safe_text(_find_path_local(dest, "xNome"))
    cnpj_destinatario = _normalizar_cnpj(cnpj_destinatario_raw)

    # total
    total = _find_path_local(inf_nfe, "total")
    icms_tot = _find_path_local(total, "ICMSTot") if total is not None else None
    vnf_text = _safe_text(_find_path_local(icms_tot, "vNF")) if icms_tot is not None else None
    valor = _parse_decimal(vnf_text, "vNF")

    inf_prot = _find_by_local_tag(root, "infProt")
    cstat = _safe_text(_find_path_local(inf_prot, "cStat")) if inf_prot is not None else None
    protocolo = _safe_text(_find_path_local(inf_prot, "nProt")) if inf_prot is not None else None
    recebimento = (
        _parse_datetime(_safe_text(_find_path_local(inf_prot, "dhRecbto")))
        if inf_prot is not None
        else None
    )
    chave_protocolo = (
        _safe_text(_find_path_local(inf_prot, "chNFe")) if inf_prot is not None else None
    )
    if cstat not in {"100", "150"} or not re.fullmatch(r"\d{15}", protocolo or ""):
        raise ValueError("NF-e sem protocolo de autorização válido (cStat 100 ou 150).")
    if recebimento is None:
        raise ValueError("NF-e sem data válida de recebimento da autorização.")
    if not chave_acesso or not re.fullmatch(r"\d{44}", chave_acesso):
        raise ValueError("Chave de acesso da NF-e deve conter exatamente 44 dígitos.")
    if chave_protocolo != chave_acesso:
        raise ValueError("Chave de acesso diverge da chave informada no protocolo.")

    # observacao
    observacao = f"Destinatário: {nome_destinatario}" if nome_destinatario else None

    if numero is None:
        raise ValueError("Campo nNF (número da NF-e) não encontrado.")
    if cnpj_emitente is None:
        raise ValueError("CNPJ do emitente não encontrado ou inválido na NF-e.")
    if data_emissao is None:
        raise ValueError("Data de emissão não encontrada ou inválida na NF-e.")

    return NotaParseada(
        tipo="nfe",
        numero=numero,
        serie=serie,
        cnpj_emitente=cnpj_emitente,
        nome_emitente=nome_emitente,
        cnpj_destinatario=cnpj_destinatario,
        valor=valor,
        data_emissao=data_emissao,
        chave_acesso=chave_acesso,
        observacao=observacao,
    )


def _parse_nfse(root: ET.Element) -> NotaParseada:
    """Extrai dados de uma NFS-e ABRASF."""

    # Localiza InfNfse
    inf_nfse = _find_by_local_tag(root, "InfNfse")

    numero_elem = _find_by_local_tag(inf_nfse, "Numero") if inf_nfse is not None else None
    numero = _safe_text(numero_elem)

    data_elem = _find_by_local_tag(inf_nfse, "DataEmissao") if inf_nfse is not None else None
    data_emissao = _parse_datetime(_safe_text(data_elem))

    # Servico/Valores
    servico = _find_by_local_tag(root, "Servico")
    valores = _find_by_local_tag(servico, "Valores") if servico is not None else None

    valor_servicos_text = _safe_text(
        _find_by_local_tag(valores, "ValorServicos") if valores is not None else None
    )
    valor = _parse_decimal(valor_servicos_text, "ValorServicos")

    valor_iss_text = _safe_text(
        _find_by_local_tag(valores, "ValorIss") if valores is not None else None
    )
    observacao = f"ISS: {valor_iss_text}" if valor_iss_text else None

    # PrestadorServico
    prestador = _find_by_local_tag(root, "PrestadorServico")
    id_prestador = _find_by_local_tag(prestador, "IdentificacaoPrestador") if prestador is not None else None
    cnpj_emitente_raw = _safe_text(
        _find_by_local_tag(id_prestador, "Cnpj") if id_prestador is not None else None
    )
    cnpj_emitente = _normalizar_cnpj(cnpj_emitente_raw)
    nome_emitente = _safe_text(
        _find_by_local_tag(prestador, "RazaoSocial") if prestador is not None else None
    )

    # TomadorServico
    tomador = _find_by_local_tag(root, "TomadorServico")
    id_tomador = _find_by_local_tag(tomador, "IdentificacaoTomador") if tomador is not None else None
    cpf_cnpj = _find_by_local_tag(id_tomador, "CpfCnpj") if id_tomador is not None else None
    cnpj_destinatario_raw = _safe_text(
        _find_by_local_tag(cpf_cnpj, "Cnpj") if cpf_cnpj is not None else None
    )
    if cnpj_destinatario_raw is None and cpf_cnpj is not None:
        cnpj_destinatario_raw = _safe_text(_find_by_local_tag(cpf_cnpj, "Cpf"))
    cnpj_destinatario = _normalizar_cnpj(cnpj_destinatario_raw)

    if numero is None:
        raise ValueError("Campo Numero da NFS-e não encontrado.")
    if cnpj_emitente is None:
        raise ValueError("CNPJ do prestador não encontrado ou inválido na NFS-e.")
    if data_emissao is None:
        raise ValueError("Data de emissão não encontrada ou inválida na NFS-e.")
    codigo_verificacao = _safe_text(_find_by_local_tag(inf_nfse, "CodigoVerificacao"))
    if not codigo_verificacao:
        raise ValueError("NFS-e sem código de verificação/autorização.")

    return NotaParseada(
        tipo="nfse",
        numero=numero,
        serie=None,
        cnpj_emitente=cnpj_emitente,
        nome_emitente=nome_emitente,
        cnpj_destinatario=cnpj_destinatario,
        valor=valor,
        data_emissao=data_emissao,
        chave_acesso=None,
        observacao=observacao,
    )


def parse_nota_xml(conteudo: bytes) -> NotaParseada:
    """Ponto de entrada principal: recebe bytes de XML e retorna NotaParseada.

    Lança ValueError se o formato não for reconhecido ou campos obrigatórios
    estiverem ausentes.
    """
    if re.search(br"<!DOCTYPE|<!ENTITY", conteudo, re.IGNORECASE):
        raise ValueError("XML com DTD ou declaração de entidade não é permitido.")
    try:
        root = ET.fromstring(conteudo)
    except ET.ParseError as exc:
        raise ValueError(f"XML inválido: {exc}") from exc

    if _is_nfe(root):
        nota = _parse_nfe(root)
        _validar_assinatura(conteudo, root, f"NFe{nota.chave_acesso}")
        return nota

    if _is_nfse(root):
        nota = _parse_nfse(root)
        inf_nfse = _find_by_local_tag(root, "InfNfse")
        referencia = inf_nfse.get("Id", "") if inf_nfse is not None else ""
        if not referencia:
            raise ValueError("NFS-e sem identificador assinado.")
        _validar_assinatura(conteudo, root, referencia)
        return nota

    raise ValueError("Formato XML não reconhecido. Esperado NF-e 4.0 ou NFS-e ABRASF.")
