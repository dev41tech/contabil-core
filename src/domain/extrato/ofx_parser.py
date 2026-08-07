"""Parser de arquivos OFX (Open Financial Exchange).

Suporta OFX 1.x (SGML não-padrão usado pelos bancos brasileiros)
e OFX 2.x (XML válido).

Não usa biblioteca externa — parse direto por regex, suficiente
para o subset de OFX que os bancos brasileiros geram.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


@dataclass
class TransacaoOFX:
    fitid: str           # ID único da transação no banco
    data: datetime
    valor: Decimal       # positivo = crédito, negativo = débito
    historico: str
    tipo_ofx: str        # CREDIT, DEBIT, etc.


class OFXParseError(Exception):
    pass


@dataclass
class OFXParseResult:
    transacoes: list[TransacaoOFX]
    total_blocos: int
    erros: list[str]


def parse_ofx(conteudo: str) -> list[TransacaoOFX]:
    """Parseia um arquivo OFX e retorna lista de transações."""
    return parse_ofx_detalhado(conteudo).transacoes


def parse_ofx_detalhado(conteudo: str) -> OFXParseResult:
    """Parseia OFX preservando a contagem e os erros de cada bloco rejeitado."""
    # Normaliza quebras de linha e remove BOM
    conteudo = conteudo.replace("\r\n", "\n").replace("\r", "\n")
    conteudo = conteudo.lstrip("\ufeff")

    # Detecta se é OFX 2.x (XML) ou 1.x (SGML).
    # OFX 2.x sempre começa com declaração <?xml …?>.
    # OFX 1.x tem cabeçalho textual (OFXHEADER:100 …) seguido de <OFX> em SGML.
    if "<?xml" in conteudo[:200]:
        return _parse_xml(conteudo)
    return _parse_sgml(conteudo)


def _parse_sgml(conteudo: str) -> OFXParseResult:
    """OFX 1.x — SGML sem fechamento de tags, formato mais comum nos bancos BR."""
    transacoes: list[TransacaoOFX] = []
    erros: list[str] = []

    # Extrai cada bloco <STMTTRN>...</STMTTRN>
    blocos = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", conteudo, re.DOTALL | re.IGNORECASE)

    if not blocos:
        # Tenta sem tag de fechamento (alguns bancos não fecham)
        blocos = re.findall(
            r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|$)",
            conteudo,
            re.DOTALL | re.IGNORECASE,
        )

    for indice, bloco in enumerate(blocos, start=1):
        try:
            transacoes.append(_parse_bloco(bloco))
        except (OFXParseError, ValueError) as exc:
            erros.append(f"Transação {indice}: {exc}")

    return OFXParseResult(transacoes=transacoes, total_blocos=len(blocos), erros=erros)


def _parse_xml(conteudo: str) -> OFXParseResult:
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

    transacoes: list[TransacaoOFX] = []
    erros: list[str] = []
    blocos = list(root.iter("STMTTRN"))
    for indice, stmttrn in enumerate(blocos, start=1):
        bloco = ET.tostring(stmttrn, encoding="unicode")
        try:
            transacoes.append(_parse_bloco(bloco))
        except (OFXParseError, ValueError) as exc:
            erros.append(f"Transação {indice}: {exc}")
    return OFXParseResult(transacoes=transacoes, total_blocos=len(blocos), erros=erros)


def _get_tag(bloco: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*([^\n<]+)", bloco, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_bloco(bloco: str) -> TransacaoOFX:
    fitid = _get_tag(bloco, "FITID")
    dtposted = _get_tag(bloco, "DTPOSTED")
    trnamt = _get_tag(bloco, "TRNAMT")
    memo = _get_tag(bloco, "MEMO") or _get_tag(bloco, "NAME") or ""
    trntype = _get_tag(bloco, "TRNTYPE") or "OTHER"

    ausentes = [
        nome for nome, valor in (("FITID", fitid), ("DTPOSTED", dtposted), ("TRNAMT", trnamt))
        if not valor
    ]
    if ausentes:
        raise OFXParseError(f"campo(s) obrigatório(s) ausente(s): {', '.join(ausentes)}")

    try:
        # Formato OFX padrão: ponto decimal ("1500.00")
        # Formato BR: separador de milhar ponto + decimal vírgula ("1.500,00")
        if "," in trnamt and "." in trnamt:
            trnamt = trnamt.replace(".", "").replace(",", ".")
        else:
            trnamt = trnamt.replace(",", ".")
        valor = Decimal(trnamt)
        if not valor.is_finite():
            raise InvalidOperation
    except InvalidOperation as exc:
        raise OFXParseError("valor TRNAMT inválido") from exc

    data = _parse_data(dtposted)
    if not data:
        raise OFXParseError(f"data DTPOSTED inválida: {dtposted}")

    return TransacaoOFX(
        fitid=fitid,
        data=data,
        valor=valor,
        historico=memo.strip(),
        tipo_ofx=trntype.upper(),
    )


def _parse_data(s: str) -> datetime | None:
    """Parseia data OFX completa, inclusive fração e offset ``[-3:BRT]``."""
    match = re.fullmatch(
        r"(?P<base>\d{8}(?:\d{4}(?:\d{2})?)?)"
        r"(?:\.(?P<fracao>\d+))?"
        r"(?:\[(?P<offset>[+-]?\d+(?:\.\d+)?):[^\]]+\])?",
        s.strip(),
    )
    if not match:
        return None
    base = match.group("base")
    formato = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(base))
    if formato is None:
        return None
    try:
        data = datetime.strptime(base, formato)
        fracao = match.group("fracao")
        if fracao:
            data = data.replace(microsecond=int((fracao + "000000")[:6]))
        offset = match.group("offset")
        tz = timezone(timedelta(hours=float(offset))) if offset is not None else UTC
        return data.replace(tzinfo=tz)
    except (ValueError, OverflowError):
        return None
