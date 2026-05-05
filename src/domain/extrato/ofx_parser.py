"""Parser de arquivos OFX (Open Financial Exchange).

Suporta OFX 1.x (SGML não-padrão usado pelos bancos brasileiros)
e OFX 2.x (XML válido).

Não usa biblioteca externa — parse direto por regex, suficiente
para o subset de OFX que os bancos brasileiros geram.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class TransacaoOFX:
    fitid: str           # ID único da transação no banco
    data: datetime
    valor: float         # positivo = crédito, negativo = débito
    historico: str
    tipo_ofx: str        # CREDIT, DEBIT, etc.


class OFXParseError(Exception):
    pass


def parse_ofx(conteudo: str) -> list[TransacaoOFX]:
    """Parseia um arquivo OFX e retorna lista de transações."""
    # Normaliza quebras de linha e remove BOM
    conteudo = conteudo.replace("\r\n", "\n").replace("\r", "\n")
    conteudo = conteudo.lstrip("\ufeff")

    # Detecta se é OFX 2.x (XML) ou 1.x (SGML).
    # OFX 2.x sempre começa com declaração <?xml …?>.
    # OFX 1.x tem cabeçalho textual (OFXHEADER:100 …) seguido de <OFX> em SGML.
    if "<?xml" in conteudo[:200]:
        return _parse_xml(conteudo)
    return _parse_sgml(conteudo)


def _parse_sgml(conteudo: str) -> list[TransacaoOFX]:
    """OFX 1.x — SGML sem fechamento de tags, formato mais comum nos bancos BR."""
    transacoes = []

    # Extrai cada bloco <STMTTRN>...</STMTTRN>
    blocos = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", conteudo, re.DOTALL | re.IGNORECASE)

    if not blocos:
        # Tenta sem tag de fechamento (alguns bancos não fecham)
        blocos = re.findall(
            r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|$)",
            conteudo,
            re.DOTALL | re.IGNORECASE,
        )

    for bloco in blocos:
        try:
            t = _parse_bloco(bloco)
            if t:
                transacoes.append(t)
        except Exception:
            continue  # transação malformada — pula mas não para o parse

    return transacoes


def _parse_xml(conteudo: str) -> list[TransacaoOFX]:
    """OFX 2.x — XML válido."""
    import xml.etree.ElementTree as ET

    try:
        # Extrai apenas o bloco OFX (ignora o cabeçalho processamento)
        match = re.search(r"<OFX>.*</OFX>", conteudo, re.DOTALL | re.IGNORECASE)
        if not match:
            raise OFXParseError("Bloco <OFX> não encontrado.")
        root = ET.fromstring(match.group())
    except ET.ParseError as e:
        raise OFXParseError(f"XML inválido: {e}") from e

    transacoes = []
    for stmttrn in root.iter("STMTTRN"):
        bloco = ET.tostring(stmttrn, encoding="unicode")
        t = _parse_bloco(bloco)
        if t:
            transacoes.append(t)
    return transacoes


def _get_tag(bloco: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*([^\n<]+)", bloco, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_bloco(bloco: str) -> TransacaoOFX | None:
    fitid = _get_tag(bloco, "FITID")
    dtposted = _get_tag(bloco, "DTPOSTED")
    trnamt = _get_tag(bloco, "TRNAMT")
    memo = _get_tag(bloco, "MEMO") or _get_tag(bloco, "NAME") or ""
    trntype = _get_tag(bloco, "TRNTYPE") or "OTHER"

    if not fitid or not dtposted or trnamt is None:
        return None

    try:
        # Formato OFX padrão: ponto decimal ("1500.00")
        # Formato BR: separador de milhar ponto + decimal vírgula ("1.500,00")
        if "," in trnamt and "." in trnamt:
            trnamt = trnamt.replace(".", "").replace(",", ".")
        else:
            trnamt = trnamt.replace(",", ".")
        valor = float(trnamt)
    except ValueError:
        return None

    data = _parse_data(dtposted)
    if not data:
        return None

    return TransacaoOFX(
        fitid=fitid,
        data=data,
        valor=valor,
        historico=memo.strip(),
        tipo_ofx=trntype.upper(),
    )


def _parse_data(s: str) -> datetime | None:
    """Parseia datas OFX: YYYYMMDDHHMMSS, YYYYMMDD, YYYYMMDDHHMMSS[tz]."""
    s = re.sub(r"\[.*\]", "", s).strip()  # remove timezone OFX ex: [−3:BRT]
    formatos = ["%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"]
    for fmt in formatos:
        try:
            return datetime.strptime(s[:len(fmt.replace("%", "xx"))], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
