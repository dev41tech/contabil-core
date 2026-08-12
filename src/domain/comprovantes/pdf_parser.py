"""Parser de comprovantes de pagamento (PIX, TED, DOC, boleto) em PDF.

Estratégia em duas camadas (para na primeira que encontrar o valor pago —
o único campo realmente obrigatório do cadastro):

  1. pdfplumber + busca por rótulo — percorre linha a linha procurando
     rótulos comuns de comprovante brasileiro (Favorecido, CPF/CNPJ, Valor
     Pago, Vencimento etc.), aceitando o valor na mesma linha do rótulo ou
     na linha seguinte (layout comum em comprovantes gerados por apps de
     banco, onde rótulo e valor ficam em linhas separadas). Grátis,
     instantâneo, determinístico.

  2. pdfplumber texto → GPT-4o-mini — fallback para quando a camada 1 não
     encontra o valor pago. Comprovantes têm layout ainda mais heterogêneo
     entre bancos/apps do que fatura de cartão, então esta camada carrega
     boa parte da cobertura real.

Diferente do extrato bancário, não há aqui uma terceira camada de OCR/
Vision: comprovantes quase sempre são gerados digitalmente (têm camada de
texto). Se nem regex nem IA encontrarem o valor pago, o chamador deve
orientar o preenchimento manual — que já é o comportamento atual do
sistema.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from src.core.config import get_settings

logger = logging.getLogger(__name__)

try:
    import pdfplumber  # type: ignore[import-untyped]
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False


class PDFParseError(Exception):
    pass


@dataclass
class ComprovantePDF:
    favorecido: str | None = None
    cpf_cnpj: str | None = None
    data_pagamento: datetime | None = None
    data_vencimento: datetime | None = None
    valor_documento: Decimal | None = None
    valor_pago: Decimal | None = None
    juros: Decimal | None = None
    multa: Decimal | None = None
    desconto: Decimal | None = None
    confianca: str = "regex"  # "regex" | "ia"


@dataclass
class _PDFBudget:
    deadline: float
    max_pages: int
    ai_calls_restantes: int

    def verificar_tempo(self) -> None:
        if time.monotonic() > self.deadline:
            raise PDFParseError("Processamento do PDF excedeu o tempo limite.")

    def consumir_chamada_ai(self) -> None:
        self.verificar_tempo()
        if self.ai_calls_restantes <= 0:
            raise PDFParseError("PDF excedeu o limite de chamadas ao serviço de IA.")
        self.ai_calls_restantes -= 1

    @property
    def timeout_restante(self) -> float:
        self.verificar_tempo()
        return max(1.0, min(30.0, self.deadline - time.monotonic()))


# ──────────────────────────────────────────────────────────── helpers gerais

def _parse_data(s: str) -> datetime | None:
    s = s.strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{2,4})", s)
    if not m:
        return None
    d, mo, y = m.groups()
    if len(y) == 2:
        y = "20" + y
    try:
        return datetime(int(y), int(mo), int(d), tzinfo=UTC)
    except ValueError:
        return None


def _parse_valor(s: str) -> Decimal | None:
    s = s.strip().replace("R$", "").replace(" ", "").replace("\xa0", "")
    negative = s.startswith("-") or s.startswith("(")
    s = s.lstrip("-(").rstrip(")")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    return -val if negative else val


_VALOR_NA_LINHA_RE = re.compile(r"R?\$?\s*(-?\d{1,3}(?:\.\d{3})*,\d{2})")
_DATA_NA_LINHA_RE = re.compile(r"(\d{2}/\d{2}/\d{2,4})")
_DOCUMENTO_RE = re.compile(
    r"(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{11}|\d{14})"
)


def _valor_na_linha(linha: str) -> Decimal | None:
    m = _VALOR_NA_LINHA_RE.search(linha)
    return _parse_valor(m.group(1)) if m else None


def _data_na_linha(linha: str) -> datetime | None:
    m = _DATA_NA_LINHA_RE.search(linha)
    return _parse_data(m.group(1)) if m else None


def _documento_na_linha(linha: str) -> str | None:
    m = _DOCUMENTO_RE.search(linha)
    return m.group(1) if m else None


def _primeiro_nao_nulo(*candidatos):
    """Primeiro valor não-None — não usar `or` aqui: Decimal('0.00') é falsy."""
    for candidato in candidatos:
        if candidato is not None:
            return candidato
    return None


def _apos_rotulo(linha: str, rotulo_re: re.Pattern[str]) -> str | None:
    """Texto que sobra na linha depois do rótulo (usado para o favorecido)."""
    m = rotulo_re.search(linha)
    if not m:
        return None
    resto = linha[m.end():].lstrip(" :\t-").strip()
    return resto or None


# ──────────────────────────────────────────────────────────── Camada 1: regex por rótulo

_ROTULO_FAVORECIDO = re.compile(
    r"favorecido|benefici[aá]rio|recebedor|pago\s+a\b|nome\s+d[oe]\s+destinat[aá]rio",
    re.IGNORECASE,
)
_ROTULO_DOCUMENTO = re.compile(r"cpf\s*/\s*cnpj|cnpj\s*/\s*cpf|\bcpf\b|\bcnpj\b", re.IGNORECASE)
_ROTULO_VALOR_DOCUMENTO = re.compile(
    r"valor\s+(?:do\s+)?documento|valor\s+nominal|valor\s+de\s+face", re.IGNORECASE
)
_ROTULO_VALOR_PAGO = re.compile(
    r"valor\s+(?:total\s+)?pago|valor\s+da\s+transa[cç][aã]o|valor\s+total\b|^valor\s*:",
    re.IGNORECASE,
)
_ROTULO_DATA_PAGAMENTO = re.compile(
    r"data\s+(?:d[oe]\s+)?pagamento|pago\s+em|data\s+da\s+transa[cç][aã]o|"
    r"data\s+d[oe]\s+cr[eé]dito|efetivad[oa]\s+em|realizado\s+em",
    re.IGNORECASE,
)
_ROTULO_VENCIMENTO = re.compile(r"vencimento", re.IGNORECASE)
_ROTULO_JUROS = re.compile(r"\bjuros\b", re.IGNORECASE)
_ROTULO_MULTA = re.compile(r"\bmulta\b", re.IGNORECASE)
_ROTULO_DESCONTO = re.compile(r"\bdesconto\b", re.IGNORECASE)


def _parse_por_regex(linhas: list[str]) -> ComprovantePDF:
    resultado = ComprovantePDF(confianca="regex")

    for i, raw in enumerate(linhas):
        linha = raw.strip()
        if not linha:
            continue
        proxima = linhas[i + 1].strip() if i + 1 < len(linhas) else ""

        if resultado.favorecido is None and _ROTULO_FAVORECIDO.search(linha):
            achado = _primeiro_nao_nulo(_apos_rotulo(linha, _ROTULO_FAVORECIDO), proxima or None)
            if achado:
                resultado.favorecido = achado[:300]

        if resultado.cpf_cnpj is None and _ROTULO_DOCUMENTO.search(linha):
            achado = _primeiro_nao_nulo(_documento_na_linha(linha), _documento_na_linha(proxima))
            if achado:
                resultado.cpf_cnpj = achado[:18]

        if resultado.valor_documento is None and _ROTULO_VALOR_DOCUMENTO.search(linha):
            resultado.valor_documento = _primeiro_nao_nulo(
                _valor_na_linha(linha), _valor_na_linha(proxima)
            )
        elif resultado.valor_pago is None and _ROTULO_VALOR_PAGO.search(linha):
            resultado.valor_pago = _primeiro_nao_nulo(
                _valor_na_linha(linha), _valor_na_linha(proxima)
            )

        if resultado.data_pagamento is None and _ROTULO_DATA_PAGAMENTO.search(linha):
            resultado.data_pagamento = _primeiro_nao_nulo(
                _data_na_linha(linha), _data_na_linha(proxima)
            )

        if resultado.data_vencimento is None and _ROTULO_VENCIMENTO.search(linha):
            resultado.data_vencimento = _primeiro_nao_nulo(
                _data_na_linha(linha), _data_na_linha(proxima)
            )

        if resultado.juros is None and _ROTULO_JUROS.search(linha):
            resultado.juros = _primeiro_nao_nulo(_valor_na_linha(linha), _valor_na_linha(proxima))

        if resultado.multa is None and _ROTULO_MULTA.search(linha):
            resultado.multa = _primeiro_nao_nulo(_valor_na_linha(linha), _valor_na_linha(proxima))

        if resultado.desconto is None and _ROTULO_DESCONTO.search(linha):
            resultado.desconto = _primeiro_nao_nulo(
                _valor_na_linha(linha), _valor_na_linha(proxima)
            )

    return resultado


# ──────────────────────────────────────────────────────────── Camada 2: AI texto

_AI_SYSTEM = (
    "Você é um extrator de dados financeiros. "
    "Responda SOMENTE com JSON puro — sem texto, sem markdown, sem explicações."
)

_AI_PROMPT = """\
Analise este comprovante de pagamento brasileiro (PIX, TED, DOC ou boleto) e extraia os dados.

Retorne um único objeto JSON com EXATAMENTE estas chaves:
  "favorecido"      → nome de quem recebeu o pagamento, ou null
  "cpf_cnpj"        → CPF ou CNPJ do favorecido (como aparece no documento), ou null
  "valor_pago"      → número decimal (ponto como separador) do valor efetivamente pago, ou null
  "valor_documento" → número decimal do valor original do documento/boleto, se diferente do
                       valor pago (ex: quando há juros/multa/desconto), ou null
  "data_pagamento"  → string DD/MM/AAAA da data em que o pagamento foi efetivado, ou null
  "data_vencimento" → string DD/MM/AAAA da data de vencimento (só existe em boleto), ou null
  "juros"           → número decimal de juros cobrados, ou null
  "multa"           → número decimal de multa cobrada, ou null
  "desconto"        → número decimal de desconto concedido, ou null

Regras obrigatórias:
- Use EXATAMENTE os nomes de campo acima
- Se um campo não aparecer no documento, use null (não invente valores)
- Valores brasileiros: "1.234,56" vira 1234.56

Exemplo de saída:
{"favorecido":"JOAO DA SILVA","cpf_cnpj":"123.456.789-00","valor_pago":150.00,"valor_documento":null,"data_pagamento":"05/01/2026","data_vencimento":null,"juros":null,"multa":null,"desconto":null}"""

_AI_TEXT_MAX_CHARS = 40_000


def _parse_ai_response(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw).strip()
    try:
        result = json.loads(raw, parse_float=Decimal)
        if isinstance(result, list) and result:
            result = result[0]
        if not isinstance(result, dict):
            logger.warning("IA retornou tipo inesperado: %s", type(result))
            return {}
        return result
    except json.JSONDecodeError as e:
        logger.warning("Falha ao parsear JSON da IA: %s", e)
        return {}


def _decimal_do_item(item: dict, chave: str) -> Decimal | None:
    valor = item.get(chave)
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def _data_do_item(item: dict, chave: str) -> datetime | None:
    valor = item.get(chave)
    if not valor:
        return None
    return _parse_data(str(valor))


def _parse_por_ai_texto(linhas: list[str], budget: _PDFBudget) -> ComprovantePDF | None:
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        logger.info("AI texto desabilitada para dados financeiros — pulando camada 2")
        return None

    texto = "\n".join(linhas)
    if len(texto) > _AI_TEXT_MAX_CHARS:
        logger.warning(
            "AI texto: texto truncado de %d para %d chars", len(texto), _AI_TEXT_MAX_CHARS
        )
        texto = texto[:_AI_TEXT_MAX_CHARS]

    budget.consumir_chamada_ai()

    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            timeout=budget.timeout_restante,
            max_tokens=1024,
            temperature=0,
            messages=[
                {"role": "system", "content": _AI_SYSTEM},
                {"role": "user", "content": f"Comprovante:\n\n{texto}\n\n{_AI_PROMPT}"},
            ],
        )
    except Exception as e:
        logger.warning("AI texto: falha na chamada OpenAI: %s", e)
        return None

    raw = response.choices[0].message.content or ""
    item = _parse_ai_response(raw)
    if not item:
        return None

    favorecido = item.get("favorecido")
    cpf_cnpj = item.get("cpf_cnpj")
    return ComprovantePDF(
        favorecido=str(favorecido)[:300] if favorecido else None,
        cpf_cnpj=str(cpf_cnpj)[:18] if cpf_cnpj else None,
        valor_pago=_decimal_do_item(item, "valor_pago"),
        valor_documento=_decimal_do_item(item, "valor_documento"),
        data_pagamento=_data_do_item(item, "data_pagamento"),
        data_vencimento=_data_do_item(item, "data_vencimento"),
        juros=_decimal_do_item(item, "juros"),
        multa=_decimal_do_item(item, "multa"),
        desconto=_decimal_do_item(item, "desconto"),
        confianca="ia",
    )


# ──────────────────────────────────────────────────────────── extração de texto do PDF

def _extrair_linhas(conteudo_bytes: bytes, budget: _PDFBudget) -> tuple[list[str], int]:
    all_lines: list[str] = []
    total_chars = 0
    try:
        with pdfplumber.open(BytesIO(conteudo_bytes)) as pdf:
            if not pdf.pages:
                raise PDFParseError("PDF sem páginas.")
            if len(pdf.pages) > budget.max_pages:
                raise PDFParseError(f"PDF excede o limite de {budget.max_pages} páginas.")
            for page in pdf.pages:
                budget.verificar_tempo()
                text = page.extract_text() or ""
                total_chars += len(text)
                all_lines.extend(text.splitlines())
    except PDFParseError:
        raise
    except Exception as e:
        logger.warning("pdfplumber: falha na extração de texto: %s", e)
        return [], 0
    return all_lines, total_chars


# ──────────────────────────────────────────────────────────── entrypoint

def parse_pdf(conteudo_bytes: bytes) -> ComprovantePDF:
    """Extrai os dados de um comprovante de pagamento em PDF.

    Camada 1 — busca por rótulo (regex)
      Grátis, instantâneo, determinístico. Cobre o layout mais comum de
      comprovante brasileiro (rótulo e valor na mesma linha ou em linhas
      consecutivas).

    Camada 2 — pdfplumber texto + GPT-4o-mini
      Fallback para quando a camada 1 não encontra o valor pago —
      comprovantes têm layout muito heterogêneo entre bancos/apps.

    Levanta `PDFParseError` se nenhuma camada encontrar o valor pago; o
    chamador deve orientar o preenchimento manual nesse caso.
    """
    if not _PDFPLUMBER_AVAILABLE:
        raise PDFParseError("pdfplumber não instalado. Execute: pip install pdfplumber")

    settings = get_settings()
    budget = _PDFBudget(
        deadline=time.monotonic() + settings.pdf_parse_timeout_seconds,
        max_pages=settings.pdf_max_pages,
        ai_calls_restantes=settings.pdf_max_ai_calls,
    )

    linhas, total_chars = _extrair_linhas(conteudo_bytes, budget)

    if total_chars < 20:
        raise PDFParseError(
            "PDF sem camada de texto reconhecível (documentos escaneados/imagem "
            "não são suportados; preencha os campos manualmente)."
        )

    resultado = _parse_por_regex(linhas)
    if resultado.valor_pago is not None:
        logger.info("PDF comprovante: extraído via regex (camada 1)")
        return resultado

    logger.info(
        "PDF comprovante: regex não encontrou o valor pago — tentando AI texto (camada 2)"
    )
    resultado_ia = _parse_por_ai_texto(linhas, budget)
    if resultado_ia is not None and resultado_ia.valor_pago is not None:
        logger.info("PDF comprovante: extraído via AI texto (camada 2)")
        return resultado_ia

    raise PDFParseError(
        "Não foi possível identificar o valor pago neste comprovante. "
        "Preencha os campos manualmente."
    )
