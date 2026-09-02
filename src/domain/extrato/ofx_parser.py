"""Parser de arquivos OFX (Open Financial Exchange).

Suporta OFX 1.x (SGML não-padrão usado pelos bancos brasileiros)
e OFX 2.x (XML válido).

Não usa biblioteca externa — parse direto por regex, suficiente
para o subset de OFX que os bancos brasileiros geram.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


@dataclass
class TransacaoOFX:
    fitid: str           # ID único da transação no banco
    # Data de calendário do lançamento, não instante. Guardar isto como datetime
    # foi o que fez a tela mostrar sempre um dia a menos: meia-noite UTC vira
    # 21h do dia anterior em Brasília, e cada consumidor decidia o fuso por conta
    # própria — o export acertava, a tela errava.
    data: date
    valor: Decimal       # positivo = crédito, negativo = débito
    historico: str
    tipo_ofx: str        # CREDIT, DEBIT, etc.
    # Saldo da conta após o lançamento, quando a origem informa. O OFX não traz
    # essa informação por lançamento; só o extrato em PDF a tem. Fica no fim,
    # com default, para não quebrar as construções posicionais existentes.
    saldo_apos: Decimal | None = None
    # Posição da linha na origem — desempata lançamentos do mesmo dia.
    ordem: int | None = None


class OFXParseError(Exception):
    pass


@dataclass
class OFXParseResult:
    transacoes: list[TransacaoOFX]
    total_blocos: int
    erros: list[str]
    # Saldo de fechamento do período, declarado pelo banco em `<LEDGERBAL>`.
    #
    # O OFX não traz saldo POR LANÇAMENTO — daí `TransacaoOFX.saldo_apos` seguir
    # nulo — mas traz o saldo do PERÍODO, e ninguém lia. Encadeado entre arquivos
    # consecutivos da mesma conta (fechamento do anterior + movimento deste =
    # fechamento deste), ele prova o que a cadeia de saldos do PDF não prova:
    # que nada foi acrescentado nem perdido. Medido nos seis extratos Sicoob de
    # jan–jun/2026: os cinco encadeáveis fecham no centavo.
    saldo_declarado: Decimal | None = None
    data_saldo: date | None = None


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
            transacoes.append(_parse_bloco(bloco, ordem=indice))
        except (OFXParseError, ValueError) as exc:
            erros.append(f"Transação {indice}: {exc}")

    saldo, data_saldo = _saldo_declarado(conteudo)
    return OFXParseResult(
        transacoes=transacoes,
        total_blocos=len(blocos),
        erros=erros,
        saldo_declarado=saldo,
        data_saldo=data_saldo,
    )


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
            transacoes.append(_parse_bloco(bloco, ordem=indice))
        except (OFXParseError, ValueError) as exc:
            erros.append(f"Transação {indice}: {exc}")

    # A leitura do saldo é por texto nos dois formatos: em XML o `<LEDGERBAL>`
    # tem a mesma forma, e a busca por texto evita duplicar a regra de escolha
    # (um único LEDGERBAL, ou nenhum) em dois lugares.
    saldo, data_saldo = _saldo_declarado(conteudo)
    return OFXParseResult(
        transacoes=transacoes,
        total_blocos=len(blocos),
        erros=erros,
        saldo_declarado=saldo,
        data_saldo=data_saldo,
    )


def _get_tag(bloco: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*([^\n<]+)", bloco, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _montar_historico(memo: str | None, name: str | None) -> str:
    """Junta os DOIS campos de descrição do OFX, nessa ordem.

    Antes daqui a regra era ``MEMO or NAME`` — e o NAME nunca era lido, porque
    todo banco que preenche um preenche o outro. Nos extratos do Sicoob isso
    custava a identidade do lançamento: o MEMO é o TIPO da operação, um
    vocabulário fechado (``PIX EMITIDO OUTRA IF``, ``CRÉD.TED-STR``), e o NAME é
    a contraparte (``Pagamento Pix 38.924.906 0001-75``, ``ARCELORMITTAL
    GONVARRI BRASIL PR``). Medido nos seis extratos de jan–jun/2026: 2.101
    lançamentos colapsavam em 17 a 28 textos por mês, e **1.103 deles (52,5%)
    traziam CPF ou CNPJ dentro do NAME** — nenhum no MEMO. É por esse documento
    que o NEO resolve contraparte, e ele era descartado na porta de entrada.

    O MEMO vem PRIMEIRO de propósito: a estratégia de casamento por substring
    procura o texto da regra dentro do histórico, então toda regra já cadastrada
    pelo texto do MEMO continua casando. Inverter a ordem também funcionaria
    para a substring, mas trocar o começo da linha muda o que o contador lê na
    varredura da fila.
    """
    memo = re.sub(r"\s+", " ", (memo or "")).strip()
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name:
        return memo
    if not memo:
        return name
    # Banco que repete a mesma informação nos dois campos não deve produzir
    # histórico com o texto duplicado.
    if name.casefold() in memo.casefold():
        return memo
    if memo.casefold() in name.casefold():
        return name
    return f"{memo} {name}"


def _valor_monetario(bruto: str) -> Decimal:
    """Decimal a partir do formato OFX padrão (``1500.00``) ou BR (``1.500,00``)."""
    texto = bruto.strip()
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    else:
        texto = texto.replace(",", ".")
    valor = Decimal(texto)
    if not valor.is_finite():
        raise InvalidOperation
    return valor


def _saldo_declarado(conteudo: str) -> tuple[Decimal | None, date | None]:
    """Saldo de fechamento do período, de ``<LEDGERBAL>``.

    Deliberadamente NÃO lê ``<AVAILBAL>``: saldo disponível desconta limite e
    bloqueio, então ele não fecha com a soma dos lançamentos e uma conferência
    apoiada nele acusaria diferença em arquivo correto.

    Arquivo com mais de um ``LEDGERBAL`` (vários extratos no mesmo envelope)
    devolve nada, em vez de escolher um: aqui o valor serve de âncora para
    conferir completude, e âncora escolhida no chute é pior que âncora nenhuma.
    """
    blocos = re.findall(
        r"<LEDGERBAL>(.*?)(?=</LEDGERBAL>|<AVAILBAL>|</STMTRS>|$)",
        conteudo,
        re.DOTALL | re.IGNORECASE,
    )
    if len(blocos) != 1:
        return None, None

    bruto = _get_tag(blocos[0], "BALAMT")
    if not bruto:
        return None, None
    try:
        saldo = _valor_monetario(bruto)
    except InvalidOperation:
        return None, None

    dtasof = _get_tag(blocos[0], "DTASOF")
    return saldo, _parse_data(dtasof) if dtasof else None


def _parse_bloco(bloco: str, ordem: int | None = None) -> TransacaoOFX:
    fitid = _get_tag(bloco, "FITID")
    dtposted = _get_tag(bloco, "DTPOSTED")
    trnamt = _get_tag(bloco, "TRNAMT")
    memo = _montar_historico(_get_tag(bloco, "MEMO"), _get_tag(bloco, "NAME"))
    trntype = _get_tag(bloco, "TRNTYPE") or "OTHER"

    ausentes = [
        nome for nome, valor in (("FITID", fitid), ("DTPOSTED", dtposted), ("TRNAMT", trnamt))
        if not valor
    ]
    if ausentes:
        raise OFXParseError(f"campo(s) obrigatório(s) ausente(s): {', '.join(ausentes)}")

    try:
        valor = _valor_monetario(trnamt)
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
        ordem=ordem,
    )


def _parse_data(s: str) -> date | None:
    """Data de calendário do DTPOSTED, no fuso que o próprio OFX declara.

    O OFX pode trazer só a data (``20260701``) ou data e hora com offset
    (``20260701220000[-3:BRT]``). No segundo caso a data de negócio é a do fuso
    declarado: 22h do dia 01 em Brasília é dia 01, embora seja dia 02 em UTC.
    Converter para UTC antes de tirar a data trocaria o dia dos lançamentos
    noturnos — por isso a conversão acontece aqui, onde o offset ainda é conhecido.
    """
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
        # A hora e o offset só existem para situar a data no fuso do banco; a
        # data resultante é o que interessa, e sai daqui já resolvida.
        return data.date()
    except (ValueError, OverflowError):
        return None
