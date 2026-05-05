"""Parser de extratos bancários em PDF.

Estratégia em três camadas:
  1. pdfplumber + regex — para PDFs com camada de texto cujo layout é reconhecido
     (Bradesco X-One e similares). Grátis, instantâneo, determinístico.

  2. pdfplumber texto → GPT-4o-mini — fallback para PDFs com camada de texto de
     bancos não mapeados (BB, Sicoob, Santander, Caixa, etc.). O texto já extraído
     é enviado como prompt; GPT-4o-mini é ~10× mais barato e rápido que Vision.
     Custo estimado: ~R$ 0,005 por extrato de 10 páginas.

  3. PyMuPDF + OpenAI Vision GPT-4o — para PDFs de imagem/vetorial sem camada de
     texto (ex: Itaú escaneado). Mais lento e caro; usado apenas quando as camadas
     1 e 2 não produzem resultado.

Retorna lista de TransacaoOFX (compatível com OFX parser).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from io import BytesIO

from src.domain.extrato.ofx_parser import TransacaoOFX
from src.core.config import get_settings

logger = logging.getLogger(__name__)

try:
    import pdfplumber  # type: ignore[import-untyped]
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    _FITZ_AVAILABLE = True
except ImportError:
    _FITZ_AVAILABLE = False


class PDFParseError(Exception):
    pass


# ──────────────────────────────────────────────────────────── helpers gerais

_DATE_FULL_RE = re.compile(r"\b(\d{2})[/\-](\d{2})[/\-](\d{2,4})\b")
_DATE_SHORT_RE = re.compile(r"\b(\d{2})[/\-](\d{2})\b")
_SALDO_RE = re.compile(r"\bSALDO\b|\bBALANCE\b|\bTOTAL\b", re.IGNORECASE)
_BARCODE_RE = re.compile(r"^\d{17,20}")   # código de barras / nosso número Bradesco


def _parse_data(s: str, referencia_ano: int | None = None) -> datetime | None:
    """Aceita DD/MM/AAAA, DD/MM/AA ou DD/MM (sem ano)."""
    s = s.strip()
    m = _DATE_FULL_RE.search(s)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        try:
            return datetime(int(y), int(mo), int(d), tzinfo=UTC)
        except ValueError:
            pass
    m2 = _DATE_SHORT_RE.search(s)
    if m2:
        d, mo = m2.group(1), m2.group(2)
        ano = referencia_ano or datetime.now(UTC).year
        try:
            return datetime(ano, int(mo), int(d), tzinfo=UTC)
        except ValueError:
            pass
    return None


def _parse_valor(s: str) -> float | None:
    s = s.strip().replace("R$", "").replace(" ", "").replace("\xa0", "")
    negative = s.startswith("-") or s.startswith("(")
    s = s.lstrip("-(").rstrip(")")
    if re.match(r"^\d{1,3}(,\d{3})*\.\d{2}$", s):
        val = float(s.replace(",", ""))
    elif "," in s and "." in s:
        val = float(s.replace(".", "").replace(",", "."))
    elif "," in s:
        val = float(s.replace(",", "."))
    else:
        try:
            val = float(s)
        except ValueError:
            return None
    return -val if negative else val


def _gerar_fitid(data: datetime, historico: str, valor: float, idx: int) -> str:
    raw = f"{data.isoformat()}{historico}{valor}{idx}"
    return "PDF" + hashlib.md5(raw.encode()).hexdigest()[:12].upper()


# ──────────────────────────────────────────────────────────── pdfplumber (extração)

# Padrão principal: qualquer linha com dois números ao final
# grupo 1 = prefixo (contém data/desc/doc)
# grupo 2 = valor da transação (pode ser negativo)
# grupo 3 = saldo corrente (descartado)
_TX_LINE = re.compile(
    r"^(.+?)\s+"
    r"([-]?\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s+"
    r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*$",
)

# Prefixo de data no início de uma linha
# NOTA: \d{4} deve vir ANTES de \d{2} para evitar que "2026" case como "20" + sobra
_DATE_PREFIX = re.compile(r"^(\d{2}[/\-]\d{2}[/\-](?:\d{4}|\d{2}))\s*")

# Linhas a ignorar sempre
_SKIP_RE = re.compile(
    r"^(Data\s|Ag[eê]ncia|Extrato|Total\s|Os dados|Pr[oó]ximo|"
    r"^\s*Cr[eé]dito|^\s*D[eé]bito|Saldo \(|REM:\s)",
    re.IGNORECASE,
)

# Limite de texto enviado para a IA (gpt-4o-mini suporta 128k tokens ≈ ~500k chars)
_AI_TEXT_MAX_CHARS = 80_000


def _extrair_linhas_pdfplumber(conteudo_bytes: bytes) -> tuple[list[str], int]:
    """Extrai todas as linhas de texto do PDF via pdfplumber.

    Retorna (linhas, total_chars). Se total_chars < 50 o PDF não tem camada de texto.
    """
    all_lines: list[str] = []
    with pdfplumber.open(BytesIO(conteudo_bytes)) as pdf:
        if not pdf.pages:
            raise PDFParseError("PDF sem páginas.")
        total_chars = sum(len(p.extract_text() or "") for p in pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_lines.extend(text.splitlines())
    return all_lines, total_chars


# ──────────────────────────────────────────────────────────── pdfplumber (regex parser)

def _parse_linhas_multipagina(
    lines: list[str], referencia_ano: int
) -> list[TransacaoOFX]:
    """Parser linha-a-linha para extratos com formato multi-linha (ex: Bradesco X-One).

    Suporta:
    - Transações PIX/TED: linha texto-puro (descrição) seguida de linha com data+doc+valor+saldo.
    - Transações PGTO:    tudo em uma linha: desc+doc+valor+saldo (sem data, herda a última vista).
    - Boletos (pág 2+):   barcode18digits+desc+doc+valor+saldo (linha de dados),
                          seguida de linha texto-puro (tipo do lançamento) → usada
                          como descrição para o PRÓXIMO lançamento da mesma série.
    """
    transacoes: list[TransacaoOFX] = []
    idx = 0
    last_date: datetime | None = None
    pending_desc: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # ── Linhas a ignorar completamente ────────────────────────────────────
        if _SKIP_RE.search(line):
            continue
        # Saldo sem débito/crédito (ex: "SALDO ANTERIOR 232,94")
        if _SALDO_RE.search(line) and not re.search(r"[-]\d", line):
            continue

        # ── Tenta casar linha com [prefixo] [valor] [saldo] ──────────────────
        m = _TX_LINE.match(line)
        if not m:
            # Linha sem números finais = texto puro → candidata a descrição
            # Remove prefixo de código de barras se presente
            clean = _BARCODE_RE.sub("", line).strip()
            if clean and not re.match(r"^\d+$", clean):
                pending_desc = clean
            continue

        prefix_raw, val_str, _saldo = m.group(1), m.group(2), m.group(3)

        # ── Extrai data do prefixo (se houver) ────────────────────────────────
        dm = _DATE_PREFIX.match(prefix_raw)
        if dm:
            parsed_date = _parse_data(dm.group(1), referencia_ano)
            if parsed_date:
                last_date = parsed_date
            rest = prefix_raw[dm.end():].strip()
        else:
            rest = prefix_raw.strip()

        # Sem data disponível → pula (não temos como datar a transação)
        if last_date is None:
            continue

        # ── Limpa o restante (remove barcode e número de documento) ───────────
        rest = _BARCODE_RE.sub("", rest).strip()
        # Remove doc number puro no início ("501", "4803" etc.)
        rest_no_doc = re.sub(r"^\d{3,8}\s*", "", rest).strip()
        # Remove doc number do fim ("PGTO FERIAS 4803" → "PGTO FERIAS")
        rest_no_doc = re.sub(r"\s+\d{4,8}\s*$", "", rest_no_doc).strip()

        # ── Histórico final ───────────────────────────────────────────────────
        # Prioridade: pending_desc > descrição da linha (sem doc)
        historico = (pending_desc or rest_no_doc or rest or "SEM DESCRIÇÃO").strip()
        pending_desc = None   # consome o pending

        # ── Valor ─────────────────────────────────────────────────────────────
        valor = _parse_valor(val_str)
        if valor is None or abs(valor) < 0.001:
            continue

        fitid = _gerar_fitid(last_date, historico, valor, idx)
        transacoes.append(
            TransacaoOFX(
                fitid=fitid,
                data=last_date,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
            )
        )
        idx += 1

    return transacoes


# ──────────────────────────────────────────────────────────── prompts compartilhados

_AI_SYSTEM = (
    "Você é um extrator de dados financeiros. "
    "Responda SOMENTE com JSON puro — sem texto, sem markdown, sem explicações."
)

_AI_PROMPT = """\
Analise este extrato bancário brasileiro e extraia TODAS as linhas de transação.

Retorne um array JSON onde cada objeto tem EXATAMENTE estas três chaves:
  "data"     → string no formato DD/MM/AAAA (ex: "15/01/2026")
  "historico"→ string com a descrição da transação (ex: "PIX RECEBIDO JOAO SILVA")
  "valor"    → número decimal (ponto como separador): NEGATIVO para débitos/saídas, POSITIVO para créditos/entradas

Regras obrigatórias:
- Use EXATAMENTE os nomes de campo: data, historico, valor (nunca date, description, amount)
- Débito / D / saída / pagamento → valor negativo
- Crédito / C / entrada / depósito / TED recebido → valor positivo
- Valores brasileiros: "1.234,56" vira 1234.56
- IGNORE linhas de saldo, subtotais, cabeçalhos e rodapés
- Se não houver transações visíveis, retorne []

Exemplo de saída:
[
  {"data":"05/01/2026","historico":"PIX ENVIADO MARIA","valor":-350.00},
  {"data":"07/01/2026","historico":"TED RECEBIDO EMPRESA XYZ","valor":1200.50}
]"""


def _parse_ai_response(raw: str) -> list[dict]:
    """Parseia a resposta JSON da IA (compartilhado entre camadas 2 e 3)."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw).strip()
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            for key in ("transacoes", "transactions", "items", "data", "extrato"):
                if key in result and isinstance(result[key], list):
                    result = result[key]
                    break
        if not isinstance(result, list):
            logger.warning("IA retornou tipo inesperado: %s", type(result))
            return []
        return result
    except json.JSONDecodeError as e:
        logger.warning("Falha ao parsear JSON da IA: %s | resposta: %.500s", e, raw)
        return []


# ──────────────────────────────────────────────────────────── Camada 2: AI texto

def _parse_por_ai_texto(linhas: list[str]) -> list[TransacaoOFX]:
    """Camada 2: envia o texto já extraído pelo pdfplumber ao GPT-4o-mini.

    Muito mais barato que Vision (~10× menos custo) porque não processa imagens.
    Usado quando o regex não reconhece o layout do banco (BB, Caixa, Sicoob, etc.).
    """
    settings = get_settings()
    if not settings.openai_enabled:
        logger.info("AI texto: OPENAI_API_KEY não configurada — pulando camada 2")
        return []

    texto = "\n".join(linhas)
    if len(texto) > _AI_TEXT_MAX_CHARS:
        logger.warning(
            "AI texto: texto truncado de %d para %d chars",
            len(texto), _AI_TEXT_MAX_CHARS,
        )
        texto = texto[:_AI_TEXT_MAX_CHARS]

    logger.info("AI texto: enviando %d chars para gpt-4o-mini", len(texto))

    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=4096,
            temperature=0,
            messages=[
                {"role": "system", "content": _AI_SYSTEM},
                {"role": "user", "content": f"Extrato bancário:\n\n{texto}\n\n{_AI_PROMPT}"},
            ],
        )
    except Exception as e:
        logger.warning("AI texto: falha na chamada OpenAI: %s", e)
        return []

    raw = response.choices[0].message.content or ""
    logger.info("AI texto raw response (primeiros 500 chars): %.500s", raw)

    items = _parse_ai_response(raw)
    transacoes: list[TransacaoOFX] = []
    for idx, item in enumerate(items):
        t = _transacao_from_ai(item, idx)
        if t:
            transacoes.append(t)

    logger.info("AI texto: %d transações extraídas", len(transacoes))
    return transacoes


# ──────────────────────────────────────────────────────────── Camada 3: OCR Vision

def _render_page_to_png(pdf_bytes: bytes, page_num: int, dpi: int = 150) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.load_page(page_num)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


def _ocr_page_with_openai(png_bytes: bytes) -> list[dict]:
    settings = get_settings()
    if not settings.openai_enabled:
        raise PDFParseError(
            "OPENAI_API_KEY não configurada. "
            "Adicione a chave no .env para processar PDFs de imagem (ex: extrato Itaú): "
            "OPENAI_API_KEY=sk-..."
        )
    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())
    img_b64 = base64.standard_b64encode(png_bytes).decode()
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        temperature=0,
        messages=[
            {"role": "system", "content": _AI_SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"}},
                {"type": "text", "text": _AI_PROMPT},
            ]},
        ],
    )
    raw = response.choices[0].message.content or ""
    logger.info("OCR raw response (primeiros 500 chars): %.500s", raw)
    return _parse_ai_response(raw)


def _parse_por_ocr(conteudo_bytes: bytes) -> list[TransacaoOFX]:
    if not _FITZ_AVAILABLE:
        raise PDFParseError("PyMuPDF não instalado. Execute: pip install pymupdf")
    doc = fitz.open(stream=conteudo_bytes, filetype="pdf")
    num_pages = doc.page_count
    doc.close()
    transacoes: list[TransacaoOFX] = []
    idx = 0
    for page_num in range(num_pages):
        logger.info("OCR: processando página %d/%d", page_num + 1, num_pages)
        try:
            png_bytes = _render_page_to_png(conteudo_bytes, page_num)
            ocr_items = _ocr_page_with_openai(png_bytes)
        except PDFParseError:
            raise
        except Exception as e:
            logger.warning("OCR: falha na página %d: %s", page_num + 1, e)
            continue
        for item in ocr_items:
            t = _transacao_from_ai(item, idx)
            if t:
                transacoes.append(t)
                idx += 1
    return transacoes


# ──────────────────────────────────────────────────────────── conversor IA → TransacaoOFX

_FIELD_DATA      = ("data", "date", "data_lancamento", "dt", "data_transacao", "transaction_date")
_FIELD_HISTORICO = ("historico", "description", "descricao", "memo", "historic",
                    "historico_extrato", "desc", "narrative", "details")
_FIELD_VALOR     = ("valor", "value", "amount", "montante", "quantia", "vlr")


def _get_field(item: dict, aliases: tuple) -> str | None:
    for key in aliases:
        if key in item and item[key] is not None and str(item[key]).strip():
            return str(item[key]).strip()
    return None


def _transacao_from_ai(item: dict, idx: int) -> TransacaoOFX | None:
    """Converte um item JSON retornado pela IA em TransacaoOFX."""
    try:
        data_str = _get_field(item, _FIELD_DATA) or ""
        data = _parse_data(data_str)
        if data is None and data_str:
            try:
                data = datetime.fromisoformat(data_str.replace("/", "-")).replace(tzinfo=UTC)
            except (ValueError, TypeError):
                pass
        if data is None:
            return None
        historico = (_get_field(item, _FIELD_HISTORICO) or "SEM DESCRIÇÃO").strip() or "SEM DESCRIÇÃO"
        valor_raw = _get_field(item, _FIELD_VALOR)
        if valor_raw is None:
            return None
        try:
            valor_clean = valor_raw.replace("R$", "").replace(" ", "").replace("\xa0", "")
            valor = float(valor_clean)
        except (TypeError, ValueError):
            valor = _parse_valor(valor_raw)
            if valor is None:
                return None
        dc_hint = str(item.get("tipo", item.get("dc", item.get("type", "")))).upper()
        if dc_hint in ("D", "DB", "DEBITO", "DEBIT", "SAIDA") and valor > 0:
            valor = -valor
        elif dc_hint in ("C", "CR", "CREDITO", "CREDIT", "ENTRADA") and valor < 0:
            valor = abs(valor)
        fitid = _gerar_fitid(data, historico, valor, idx)
        return TransacaoOFX(
            fitid=fitid,
            data=data,
            valor=valor,
            historico=historico[:200],
            tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
        )
    except Exception as e:
        logger.warning("IA: erro ao converter item %s: %s", item, e)
        return None


# ──────────────────────────────────────────────────────────── entrypoint

def parse_pdf(conteudo_bytes: bytes) -> list[TransacaoOFX]:
    """Extrai transações de um PDF de extrato bancário.

    Fluxo em três camadas (para-na-primeira-que-funcionar):

      Camada 1 — pdfplumber + regex
        Grátis, instantâneo, determinístico.
        Funciona para bancos com layout mapeado (Bradesco X-One, etc.).

      Camada 2 — pdfplumber texto + GPT-4o-mini
        Ativada quando o PDF tem texto mas o regex não extrai transações.
        Custo estimado: ~R$ 0,005 por extrato de 10 páginas.
        Funciona para qualquer banco com camada de texto (BB, Caixa, Sicoob, Santander…).

      Camada 3 — PyMuPDF + GPT-4o Vision
        Ativada apenas para PDFs sem camada de texto (escaneados/vetoriais puros).
        Custo estimado: ~R$ 0,10–0,30 por extrato de 10 páginas.
        Exemplo: Itaú em PDF imagem.
    """
    if not _PDFPLUMBER_AVAILABLE and not _FITZ_AVAILABLE:
        raise PDFParseError(
            "Nenhuma biblioteca PDF disponível. "
            "Execute: pip install pdfplumber pymupdf"
        )

    # ── Camadas 1 e 2: requerem pdfplumber ───────────────────────────────────
    if _PDFPLUMBER_AVAILABLE:
        try:
            linhas, total_chars = _extrair_linhas_pdfplumber(conteudo_bytes)
        except PDFParseError:
            raise
        except Exception as e:
            logger.warning("pdfplumber: falha na extração de texto: %s", e)
            linhas, total_chars = [], 0

        if total_chars >= 50:
            logger.info("pdfplumber: %d chars extraídos", total_chars)

            # ── Camada 1: regex ───────────────────────────────────────────────
            referencia_ano = datetime.now(UTC).year
            transacoes = _parse_linhas_multipagina(linhas, referencia_ano)
            if transacoes:
                logger.info("PDF parser: %d transações via pdfplumber regex (camada 1)", len(transacoes))
                return transacoes

            logger.info(
                "pdfplumber regex: 0 transações — banco não mapeado, "
                "tentando AI texto (camada 2)"
            )

            # ── Camada 2: AI texto ────────────────────────────────────────────
            transacoes = _parse_por_ai_texto(linhas)
            if transacoes:
                logger.info("PDF parser: %d transações via AI texto (camada 2)", len(transacoes))
                return transacoes

            logger.info(
                "AI texto: 0 transações — pode ser layout complexo, "
                "tentando OCR Vision (camada 3)"
            )
        else:
            logger.info(
                "pdfplumber: PDF sem camada de texto (%d chars) — "
                "indo direto para OCR Vision (camada 3)",
                total_chars,
            )

    # ── Camada 3: OCR Vision ──────────────────────────────────────────────────
    if not _FITZ_AVAILABLE:
        raise PDFParseError(
            "PyMuPDF não instalado e nenhuma camada anterior funcionou. "
            "Execute: pip install pymupdf"
        )

    logger.info("PDF parser: usando OCR Vision GPT-4o (camada 3)")
    try:
        transacoes = _parse_por_ocr(conteudo_bytes)
    except PDFParseError:
        raise
    except Exception as e:
        raise PDFParseError(f"OCR Vision falhou: {e}") from e

    if not transacoes:
        raise PDFParseError(
            "Nenhuma transação encontrada no PDF. "
            "Verifique se o arquivo é um extrato bancário válido."
        )

    logger.info("PDF parser: %d transações via OCR Vision (camada 3)", len(transacoes))
    return transacoes
