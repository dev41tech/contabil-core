"""Parser de faturas de cartão de crédito em PDF.

Estratégia em duas camadas (para na primeira que funcionar):
  1. pdfplumber + regex — layout genérico de fatura brasileira
     (data + descrição + [parcela] + valor). Grátis, instantâneo,
     determinístico.
  2. pdfplumber texto → GPT-4o-mini — fallback para bandeiras/emissores
     cujo layout a camada 1 não reconhece.

Diferente do extrato bancário (`src/domain/extrato/pdf_parser.py`), faturas
de cartão são praticamente sempre geradas digitalmente (têm camada de
texto) — por isso não há aqui uma terceira camada de OCR/Vision para
documentos escaneados.
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
class LancamentoPDF:
    data_compra: datetime
    descricao: str
    valor: Decimal
    parcela_atual: int | None = None
    parcela_total: int | None = None


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

_MESES_ABREV = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def _mes_do_texto(mes_str: str) -> int | None:
    """"12" ou "dez" (abreviação PT-BR, como o Sicredi usa: "16/dez")."""
    if mes_str.isdigit():
        mo = int(mes_str)
        return mo if 1 <= mo <= 12 else None
    return _MESES_ABREV.get(mes_str.lower())


def _resolve_ano(mes_transacao: int, referencia: tuple[int, int] | None) -> int:
    if referencia is None:
        return datetime.now(UTC).year
    ano_vencimento, mes_vencimento = referencia
    # Fatura cruzando virada de ano: transação de mês posterior ao mês de
    # vencimento (ex: transação em dezembro, vencimento em janeiro) é do
    # ano anterior ao do vencimento.
    if mes_transacao > mes_vencimento:
        return ano_vencimento - 1
    return ano_vencimento


def _parse_data(s: str, referencia: tuple[int, int] | None = None) -> datetime | None:
    """Aceita DD/MM/AAAA, DD/MM/AA, DD/MM ou DD/MMM (mês abreviado PT-BR, ex:
    "16/dez" — layout Sicredi, sem ano nem separador numérico de mês).

    `referencia` é (ano_vencimento, mes_vencimento) da fatura, usado só
    quando o texto não traz o ano — ver `_resolve_ano`.
    """
    s = s.strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{2,4})$", s)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        try:
            return datetime(int(y), int(mo), int(d), tzinfo=UTC)
        except ValueError:
            return None
    m2 = re.match(r"^(\d{2})/([a-zç0-9]{2,3})$", s, re.IGNORECASE)
    if m2:
        d, mes_str = m2.groups()
        mo = _mes_do_texto(mes_str)
        if mo is None:
            return None
        ano = _resolve_ano(mo, referencia)
        try:
            return datetime(ano, mo, int(d), tzinfo=UTC)
        except ValueError:
            return None
    return None


_VENCIMENTO_RE = re.compile(r"vencimento\s+(\d{2})/(\d{2})/(\d{4})", re.IGNORECASE)


def _referencia_fatura(linhas: list[str]) -> tuple[int, int] | None:
    """(ano, mês) de vencimento da fatura, se aparecer em alguma linha —
    usado como âncora pra resolver o ano de transações sem ano explícito
    quando a fatura cruza virada de ano (compras de nov/dez, vencimento em
    jan). Sem isso, `_resolve_ano` cai no ano corrente na hora do import."""
    texto = "\n".join(linhas)
    m = _VENCIMENTO_RE.search(texto)
    if not m:
        return None
    _dia, mes, ano = m.groups()
    return int(ano), int(mes)


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


# ──────────────────────────────────────────────────────────── Camada 1: regex

# Linha típica de fatura: "DD/MM[/AAAA]  [HH:MM]  DESCRICAO  [NN/NN]  VALOR"
# Mês pode vir numérico (Itaú/Nubank etc: "05/03") ou abreviado PT-BR
# (Sicredi: "16/dez"). Hora colada na data (Sicredi: "16/dez 08:08") é
# descartada — não faz parte da descrição do lançamento. Valor aceita "R$"
# e sinal antes OU depois do "R$" ("-R$ 39.249,28"); `_parse_valor` já
# normaliza qualquer uma dessas formas.
_LINHA_RE = re.compile(
    r"^(?P<data>\d{2}/(?:\d{2}|[a-zç]{3})(?:/\d{2,4})?)\s+"
    r"(?:\d{2}:\d{2}\s+)?"
    r"(?P<descricao>.+?)"
    r"(?:\s+(?P<parc_atual>\d{1,2})/(?P<parc_total>\d{1,2}))?"
    r"\s+(?P<valor>-?\s*(?:R\$\s*)?-?\d{1,3}(?:\.\d{3})*,\d{2})\s*$",
    re.IGNORECASE,
)

_SKIP_RE = re.compile(
    r"^(Total|Sub-?total|Saldo|Limite|Vencimento|Fechamento|Data\s|Lan[cç]amento|"
    r"P[aá]gina|Fatura|Cart[aã]o|Pagamento\s+m[ií]nimo)",
    re.IGNORECASE,
)

_AI_TEXT_MAX_CHARS = 80_000


def _extrair_linhas(conteudo_bytes: bytes, budget: _PDFBudget) -> tuple[list[str], int]:
    all_lines: list[str] = []
    total_chars = 0
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
    return all_lines, total_chars


def _parse_por_regex(
    linhas: list[str], referencia: tuple[int, int] | None
) -> list[LancamentoPDF]:
    lancamentos: list[LancamentoPDF] = []
    for raw in linhas:
        line = raw.strip()
        if not line or _SKIP_RE.match(line):
            continue
        m = _LINHA_RE.match(line)
        if not m:
            continue
        data = _parse_data(m.group("data"), referencia)
        if data is None:
            continue
        valor = _parse_valor(m.group("valor"))
        # Valor negativo é pagamento/crédito da fatura anterior (ex: "-R$
        # 39.249,28"), não uma compra — importar como positivo inflaria o
        # total da fatura e duplicaria algo que já é rastreado por outro
        # caminho (conciliação bancária), não como lançamento de cartão.
        if valor is None or valor <= 0:
            continue
        descricao = m.group("descricao").strip()
        if not descricao:
            continue
        parc_atual = int(m.group("parc_atual")) if m.group("parc_atual") else None
        parc_total = int(m.group("parc_total")) if m.group("parc_total") else None
        lancamentos.append(
            LancamentoPDF(
                data_compra=data,
                descricao=descricao[:200],
                valor=valor,
                parcela_atual=parc_atual,
                parcela_total=parc_total,
            )
        )
    return lancamentos


# ──────────────────────────────────────────────────────────── Camada 2: AI texto

_AI_SYSTEM = (
    "Você é um extrator de dados financeiros. "
    "Responda SOMENTE com JSON puro — sem texto, sem markdown, sem explicações."
)

_AI_PROMPT = """\
Analise esta fatura de cartão de crédito brasileira e extraia TODAS as compras/lançamentos.

Retorne um array JSON onde cada objeto tem EXATAMENTE estas chaves:
  "data"          → string no formato DD/MM/AAAA (ex: "15/01/2026")
  "descricao"     → string com a descrição/estabelecimento (ex: "MERCADO LIVRE*COMPRA")
  "valor"         → número decimal positivo (ponto como separador)
  "parcela_atual" → número inteiro da parcela atual, ou null se compra à vista
  "parcela_total" → número inteiro total de parcelas, ou null se compra à vista

Regras obrigatórias:
- Use EXATAMENTE os nomes de campo acima
- IGNORE linhas de total, subtotal, saldo, limite, cabeçalhos e rodapés
- IGNORE o próprio pagamento da fatura anterior (ex: "PAGAMENTO EFETUADO")
- Se não houver lançamentos visíveis, retorne []

Exemplo de saída:
[
  {"data":"05/01/2026","descricao":"MERCADO LIVRE*COMPRA","valor":150.00,"parcela_atual":null,"parcela_total":null},
  {"data":"07/01/2026","descricao":"UBER *TRIP","valor":45.90,"parcela_atual":2,"parcela_total":3}
]"""


def _parse_ai_response(raw: str) -> list[dict]:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw).strip()
    try:
        result = json.loads(raw, parse_float=Decimal)
        if isinstance(result, dict):
            for key in ("lancamentos", "compras", "items", "data"):
                if key in result and isinstance(result[key], list):
                    result = result[key]
                    break
        if not isinstance(result, list):
            logger.warning("IA retornou tipo inesperado: %s", type(result))
            return []
        return result
    except json.JSONDecodeError as e:
        logger.warning("Falha ao parsear JSON da IA: %s", e)
        return []


def _lancamento_from_ai(item: dict) -> LancamentoPDF | None:
    try:
        data_str = str(item.get("data") or "").strip()
        data = _parse_data(data_str)
        if data is None:
            return None
        descricao = str(item.get("descricao") or "").strip() or "SEM DESCRIÇÃO"
        valor_raw = item.get("valor")
        if valor_raw is None:
            return None
        valor = Decimal(str(valor_raw)).copy_abs()
        if valor == 0:
            return None
        parc_atual_raw = item.get("parcela_atual")
        parc_total_raw = item.get("parcela_total")
        return LancamentoPDF(
            data_compra=data,
            descricao=descricao[:200],
            valor=valor,
            parcela_atual=int(parc_atual_raw) if parc_atual_raw else None,
            parcela_total=int(parc_total_raw) if parc_total_raw else None,
        )
    except (TypeError, InvalidOperation, ValueError):
        return None


def _parse_por_ai_texto(linhas: list[str], budget: _PDFBudget) -> list[LancamentoPDF]:
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        logger.info("AI texto desabilitada para dados financeiros — pulando camada 2")
        return []

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
            max_tokens=4096,
            temperature=0,
            messages=[
                {"role": "system", "content": _AI_SYSTEM},
                {"role": "user", "content": f"Fatura de cartão:\n\n{texto}\n\n{_AI_PROMPT}"},
            ],
        )
    except Exception as e:
        logger.warning("AI texto: falha na chamada OpenAI: %s", e)
        return []

    raw = response.choices[0].message.content or ""
    items = _parse_ai_response(raw)
    lancamentos: list[LancamentoPDF] = []
    for item in items:
        lanc = _lancamento_from_ai(item)
        if lanc is not None:
            lancamentos.append(lanc)

    logger.info("AI texto: %d lançamentos extraídos", len(lancamentos))
    return lancamentos


# ──────────────────────────────────────────────────────────── reconciliação

_TOTAL_RE = re.compile(
    r"(TOTAL\s+(?:DESTA\s+)?FATURA|VALOR\s+TOTAL(?:\s+DA\s+FATURA)?|TOTAL\s+A\s+PAGAR)"
    r".{0,20}?(-?\d{1,3}(?:\.\d{3})*,\d{2})",
    re.IGNORECASE,
)


def _validar_total_declarado(linhas: list[str], lancamentos: list[LancamentoPDF]) -> None:
    """Se a fatura declarar um total reconhecível, confere contra a soma extraída.

    Faturas de cartão variam muito mais de layout entre bandeiras/emissores do
    que extratos bancários — por isso, diferente do extrato, a ausência de um
    total reconhecível NÃO bloqueia a importação. Só rejeitamos quando
    encontramos um total declarado e ele diverge da soma extraída, o que é
    sinal de extração parcial.
    """
    texto = "\n".join(linhas)
    m = _TOTAL_RE.search(texto)
    if not m:
        return
    declarado = _parse_valor(m.group(2))
    if declarado is None:
        return
    soma = sum((lanc.valor for lanc in lancamentos), Decimal("0"))
    if abs(soma - abs(declarado)) > Decimal("0.05"):
        raise PDFParseError(
            f"Extração incompleta: soma dos lançamentos (R$ {soma}) não bate com "
            f"o total declarado na fatura (R$ {abs(declarado)})."
        )


# ──────────────────────────────────────────────────────────── entrypoint

def parse_pdf(conteudo_bytes: bytes) -> list[LancamentoPDF]:
    """Extrai lançamentos de uma fatura de cartão em PDF.

    Camada 1 — pdfplumber + regex
      Grátis, instantâneo, determinístico. Reconhece o layout genérico de
      fatura brasileira (data + descrição + [parcela] + valor).

    Camada 2 — pdfplumber texto + GPT-4o-mini
      Fallback para bandeiras/emissores cujo layout a camada 1 não
      reconhece. Faturas de cartão têm formato mais heterogêneo entre
      emissores do que extratos bancários, então esta camada carrega boa
      parte da cobertura real.
    """
    if not _PDFPLUMBER_AVAILABLE:
        raise PDFParseError("pdfplumber não instalado. Execute: pip install pdfplumber")

    settings = get_settings()
    budget = _PDFBudget(
        deadline=time.monotonic() + settings.pdf_parse_timeout_seconds,
        max_pages=settings.pdf_max_pages,
        ai_calls_restantes=settings.pdf_max_ai_calls,
    )

    try:
        linhas, total_chars = _extrair_linhas(conteudo_bytes, budget)
    except PDFParseError:
        raise
    except Exception as e:
        logger.warning("pdfplumber: falha na extração de texto: %s", e)
        linhas, total_chars = [], 0

    if total_chars < 50:
        raise PDFParseError(
            "PDF sem camada de texto reconhecível (documentos escaneados/imagem "
            "não são suportados para fatura de cartão; envie via CSV/Excel)."
        )

    referencia = _referencia_fatura(linhas)
    lancamentos = _parse_por_regex(linhas, referencia)
    if lancamentos:
        _validar_total_declarado(linhas, lancamentos)
        logger.info("PDF fatura: %d lançamentos via regex (camada 1)", len(lancamentos))
        return lancamentos

    logger.info("PDF fatura: regex não reconheceu o layout — tentando AI texto (camada 2)")
    lancamentos = _parse_por_ai_texto(linhas, budget)
    if lancamentos:
        _validar_total_declarado(linhas, lancamentos)
        logger.info("PDF fatura: %d lançamentos via AI texto (camada 2)", len(lancamentos))
        return lancamentos

    raise PDFParseError(
        "Não foi possível extrair lançamentos desta fatura. Verifique o arquivo "
        "ou importe via CSV/Excel."
    )
