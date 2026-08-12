"""Parser visual de notas fiscais (DANFe) a partir de PDF ou imagem — OCR/Vision.

Diferente do parser de XML (`xml_parser.py`), aqui não há assinatura digital pra
verificar: o conteúdo extraído de um PDF/imagem de DANFe é só uma leitura visual,
sem garantia criptográfica de autenticidade. Por isso toda nota importada por este
caminho deve ser persistida com `origem="ocr"` pelo chamador (`NotaService`),
diferente da nota XML (`origem="xml_assinado"`).

Estratégia em camadas (para na primeira que produzir os campos obrigatórios):

  1. pdfplumber texto → GPT-4o-mini — para PDF com camada de texto (muitos DANFe
     são gerados digitalmente, não escaneados). Não existe camada de regex aqui:
     ao contrário de extrato bancário (poucos bancos) ou fatura de cartão (um
     emissor por vez), o layout de DANFe varia por dezenas de softwares fiscais
     diferentes — regex não generalizaria.

  2. PyMuPDF + GPT-4o Vision — para PDF sem camada de texto (escaneado) ou para
     upload direto de imagem (PNG/JPG).

Gated por `allow_financial_data_to_openai`, igual aos outros parsers de PDF do
sistema (extrato, comprovantes, cartões).

Os campos extraídos passam por validação de dígito verificador de CNPJ
(`src/core/cnpj.py`) — mais rigoroso que o parser de XML, como compensação por não
ter assinatura criptográfica: um CNPJ com dígito verificador errado é descartado
(vira nota rejeitada) em vez de persistido, para não deixar erro de OCR virar dado
contábil errado silenciosamente.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from src.core.cnpj import formatar as formatar_cnpj, somente_digitos, valido as cnpj_valido
from src.core.config import get_settings
from src.domain.notas.xml_parser import NotaParseada

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


class VisualParseError(Exception):
    pass


@dataclass
class _Budget:
    deadline: float
    max_pages: int
    ai_calls_restantes: int

    def verificar_tempo(self) -> None:
        if time.monotonic() > self.deadline:
            raise VisualParseError("Processamento do arquivo excedeu o tempo limite.")

    def consumir_chamada_ai(self) -> None:
        self.verificar_tempo()
        if self.ai_calls_restantes <= 0:
            raise VisualParseError("Arquivo excedeu o limite de chamadas ao serviço de IA.")
        self.ai_calls_restantes -= 1

    @property
    def timeout_restante(self) -> float:
        self.verificar_tempo()
        return max(1.0, min(30.0, self.deadline - time.monotonic()))


def _novo_budget() -> _Budget:
    settings = get_settings()
    return _Budget(
        deadline=time.monotonic() + settings.pdf_parse_timeout_seconds,
        max_pages=settings.pdf_max_pages,
        ai_calls_restantes=settings.pdf_max_ai_calls,
    )


# ──────────────────────────────────────────────────────────── prompt compartilhado

_AI_SYSTEM = (
    "Você é um extrator de dados de documentos fiscais brasileiros. "
    "Responda SOMENTE com JSON puro — sem texto, sem markdown, sem explicações."
)

_AI_PROMPT = """\
Analise esta nota fiscal brasileira (DANFe de NF-e, ou NFS-e) e extraia os dados.

Retorne um único objeto JSON com EXATAMENTE estas chaves:
  "tipo"              → "nfe" se for NF-e (mercadoria/produto) ou "nfse" se for NFS-e (serviço)
  "numero"            → número da nota fiscal (como aparece no documento), ou null
  "serie"             → série da nota, ou null
  "cnpj_emitente"      → CNPJ de quem emitiu a nota (como aparece no documento), ou null
  "nome_emitente"      → razão social de quem emitiu, ou null
  "cnpj_destinatario"  → CNPJ ou CPF de quem recebeu/tomou o serviço, ou null
  "valor"              → número decimal (ponto como separador) do valor total da nota, ou null
  "data_emissao"       → string DD/MM/AAAA da data de emissão, ou null
  "chave_acesso"       → chave de acesso de 44 dígitos impressa no documento (só existe em NF-e), ou null

Regras obrigatórias:
- Use EXATAMENTE os nomes de campo acima
- Se um campo não aparecer no documento, use null (não invente valores)
- Valores brasileiros: "1.234,56" vira 1234.56
- chave_acesso: só dígitos, sem espaços

Exemplo de saída:
{"tipo":"nfe","numero":"12345","serie":"1","cnpj_emitente":"12.345.678/0001-90","nome_emitente":"EMPRESA LTDA","cnpj_destinatario":"98.765.432/0001-10","valor":1500.00,"data_emissao":"05/01/2026","chave_acesso":"35260112345678000190550010000123451234567890"}"""

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
        logger.warning("Falha ao parsear JSON da IA: %s (nota visual)", e)
        return {}


def _parse_data_str(s: object) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
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


def _cnpj_valido_formatado(valor: object) -> str | None:
    if not valor:
        return None
    digitos = somente_digitos(str(valor))
    if not cnpj_valido(digitos):
        return None
    return formatar_cnpj(digitos)


def _nota_from_item(item: dict) -> NotaParseada:
    """Converte o dict retornado pela IA em `NotaParseada` — mesmo shape usado pelo
    parser de XML — validando os campos obrigatórios."""
    tipo_raw = str(item.get("tipo") or "").strip().lower()
    if tipo_raw not in ("nfe", "nfse"):
        raise ValueError("Não foi possível identificar se é NF-e ou NFS-e no documento.")

    numero = str(item.get("numero") or "").strip() or None
    if numero is None:
        raise ValueError("Número da nota não encontrado no documento.")

    cnpj_emitente = _cnpj_valido_formatado(item.get("cnpj_emitente"))
    if cnpj_emitente is None:
        raise ValueError(
            "CNPJ do emitente não encontrado ou com dígito verificador inválido no documento."
        )

    valor_raw = item.get("valor")
    if valor_raw is None:
        raise ValueError("Valor da nota não encontrado no documento.")
    try:
        valor = Decimal(str(valor_raw))
    except InvalidOperation as exc:
        raise ValueError("Valor da nota inválido.") from exc
    if not valor.is_finite() or valor <= 0:
        raise ValueError("Valor da nota deve ser positivo.")

    data_emissao = _parse_data_str(item.get("data_emissao"))
    if data_emissao is None:
        raise ValueError("Data de emissão não encontrada ou inválida no documento.")

    serie = str(item.get("serie")).strip() if item.get("serie") else None
    nome_emitente = str(item.get("nome_emitente")).strip() if item.get("nome_emitente") else None
    cnpj_destinatario = _cnpj_valido_formatado(item.get("cnpj_destinatario"))

    chave_raw = item.get("chave_acesso")
    chave_acesso = None
    if chave_raw:
        digitos_chave = re.sub(r"\D", "", str(chave_raw))
        if len(digitos_chave) == 44:
            chave_acesso = digitos_chave

    return NotaParseada(
        tipo=tipo_raw,
        numero=numero,
        serie=serie,
        cnpj_emitente=cnpj_emitente,
        nome_emitente=nome_emitente,
        cnpj_destinatario=cnpj_destinatario,
        valor=valor,
        data_emissao=data_emissao,
        chave_acesso=chave_acesso,
        observacao=(
            "Extraído por OCR/Vision a partir de PDF/imagem — sem verificação "
            "criptográfica de assinatura."
        ),
    )


# ──────────────────────────────────────────────────────────── Camada 1: texto + AI

def _extrair_texto_pdf(conteudo_bytes: bytes, budget: _Budget) -> tuple[str, int]:
    with pdfplumber.open(BytesIO(conteudo_bytes)) as pdf:
        if not pdf.pages:
            raise VisualParseError("PDF sem páginas.")
        if len(pdf.pages) > budget.max_pages:
            raise VisualParseError(f"PDF excede o limite de {budget.max_pages} páginas.")
        partes: list[str] = []
        total_chars = 0
        for page in pdf.pages:
            budget.verificar_tempo()
            text = page.extract_text() or ""
            total_chars += len(text)
            partes.append(text)
    return "\n".join(partes), total_chars


def _extrair_via_texto(texto: str, budget: _Budget) -> dict:
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        logger.info("AI texto desabilitada para dados financeiros — pulando camada 1 (nota visual)")
        return {}

    if len(texto) > _AI_TEXT_MAX_CHARS:
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
                {"role": "user", "content": f"Nota fiscal:\n\n{texto}\n\n{_AI_PROMPT}"},
            ],
        )
    except Exception as e:
        logger.warning("AI texto: falha na chamada OpenAI (nota visual): %s", e)
        return {}

    raw = response.choices[0].message.content or ""
    return _parse_ai_response(raw)


# ──────────────────────────────────────────────────────────── Camada 2: Vision

def _render_page_to_png(pdf_bytes: bytes, page_num: int, dpi: int = 200) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.load_page(page_num)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


def _extrair_via_vision(img_bytes: bytes, mime_type: str, budget: _Budget) -> dict:
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        raise VisualParseError(
            "OCR externo desabilitado. Configure consentimento explícito para enviar "
            "dados financeiros ao provedor de IA."
        )
    budget.consumir_chamada_ai()
    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())
    img_b64 = base64.standard_b64encode(img_bytes).decode()
    response = client.chat.completions.create(
        model="gpt-4o",
        timeout=budget.timeout_restante,
        max_tokens=1024,
        temperature=0,
        messages=[
            {"role": "system", "content": _AI_SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}", "detail": "high"}},
                {"type": "text", "text": _AI_PROMPT},
            ]},
        ],
    )
    raw = response.choices[0].message.content or ""
    logger.info("Vision: resposta recebida com %d chars (nota visual)", len(raw))
    return _parse_ai_response(raw)


def _parse_pdf_via_vision(conteudo_bytes: bytes, budget: _Budget) -> NotaParseada:
    if not _FITZ_AVAILABLE:
        raise VisualParseError("PyMuPDF não instalado. Execute: pip install pymupdf")
    doc = fitz.open(stream=conteudo_bytes, filetype="pdf")
    num_pages = doc.page_count
    doc.close()
    if num_pages > budget.max_pages:
        raise VisualParseError(f"PDF excede o limite de {budget.max_pages} páginas.")

    ultimo_erro: ValueError | None = None
    for page_num in range(num_pages):
        budget.verificar_tempo()
        logger.info("Vision: processando página %d/%d (nota visual)", page_num + 1, num_pages)
        png_bytes = _render_page_to_png(conteudo_bytes, page_num)
        item = _extrair_via_vision(png_bytes, "image/png", budget)
        try:
            return _nota_from_item(item)
        except ValueError as exc:
            ultimo_erro = exc
            continue
    raise VisualParseError(
        str(ultimo_erro) if ultimo_erro else "Não foi possível extrair os dados da nota fiscal deste PDF."
    )


# ──────────────────────────────────────────────────────────── entrypoints

def parse_pdf(conteudo_bytes: bytes) -> NotaParseada:
    """Extrai os dados de uma nota fiscal a partir de um PDF (DANFe).

    Camada 1 — pdfplumber texto + GPT-4o-mini
      Tentada primeiro quando o PDF tem camada de texto.

    Camada 2 — PyMuPDF + GPT-4o Vision
      Ativada quando o PDF não tem camada de texto (escaneado), ou quando a
      camada 1 não extraiu os campos obrigatórios.

    Levanta `VisualParseError` se nenhuma camada extrair os campos mínimos
    (tipo, número, CNPJ do emitente válido, valor, data de emissão).
    """
    budget = _novo_budget()

    if _PDFPLUMBER_AVAILABLE:
        try:
            texto, total_chars = _extrair_texto_pdf(conteudo_bytes, budget)
        except VisualParseError:
            raise
        except Exception as e:
            logger.warning("pdfplumber: falha na extração de texto (nota visual): %s", e)
            texto, total_chars = "", 0

        if total_chars >= 20:
            item = _extrair_via_texto(texto, budget)
            try:
                return _nota_from_item(item)
            except ValueError as exc:
                logger.info(
                    "Nota visual: AI texto não extraiu campos obrigatórios (%s) — tentando Vision",
                    exc,
                )

    return _parse_pdf_via_vision(conteudo_bytes, budget)


def parse_imagem(conteudo_bytes: bytes, content_type: str = "image/png") -> NotaParseada:
    """Extrai os dados de uma nota fiscal a partir de uma imagem (PNG/JPG).

    Sem camada de texto local possível — vai direto para Vision.
    """
    budget = _novo_budget()
    item = _extrair_via_vision(conteudo_bytes, content_type, budget)
    try:
        return _nota_from_item(item)
    except ValueError as exc:
        raise VisualParseError(str(exc)) from exc
