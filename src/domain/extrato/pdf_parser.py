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
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from src.core.config import get_settings
from src.domain.extrato import bancos
from src.domain.extrato._comum import Bloco, ordenar_do_mais_antigo
from src.domain.extrato._comum import gerar_fitid as _gerar_fitid
from src.domain.extrato._comum import parse_data as _parse_data
from src.domain.extrato._comum import parse_valor as _parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

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


@dataclass
class _PDFBudget:
    deadline: float
    max_pages: int
    ai_calls_restantes: int

    def verificar_tempo(self) -> None:
        if time.monotonic() > self.deadline:
            raise PDFParseError("Processamento do PDF excedeu o tempo limite.")

    def consumir_chamada_ai(self, quantas: int = 1) -> None:
        self.verificar_tempo()
        if self.ai_calls_restantes < quantas:
            raise PDFParseError("PDF excedeu o limite de chamadas ao serviço de IA.")
        self.ai_calls_restantes -= quantas

    def estender_deadline(self, segundos: float) -> None:
        """Dá a uma camada o prazo que o perfil dela exige.

        A camada 3 gasta uma chamada por página; as outras, no máximo uma no
        arquivo inteiro. Enquanto as duas dividiam o mesmo relógio, o teto que
        sobrava para o caminho de imagem era o de um caminho que não é o dele.
        Só estende para frente: nunca encurta um prazo já concedido.
        """
        self.deadline = max(self.deadline, time.monotonic() + segundos)

    def timeout_chamada(self, maximo: float = 30.0) -> float:
        """Teto de UMA chamada, sem nunca ultrapassar o prazo do arquivo."""
        self.verificar_tempo()
        return max(1.0, min(maximo, self.deadline - time.monotonic()))

    @property
    def timeout_restante(self) -> float:
        return self.timeout_chamada()


# ──────────────────────────────────────────────────────────── helpers gerais

_DATE_FULL_RE = re.compile(r"\b(\d{2})[/\-](\d{2})[/\-](\d{2,4})\b")
_DATE_SHORT_RE = re.compile(r"\b(\d{2})[/\-](\d{2})\b")
_SALDO_RE = re.compile(r"\bSALDO\b|\bBALANCE\b|\bTOTAL\b", re.IGNORECASE)
_BARCODE_RE = re.compile(r"^\d{17,20}")   # código de barras / nosso número Bradesco


# ──────────────────────────────────────────────────────────── pdfplumber (extração)

# Padrão principal: qualquer linha com dois números ao final
# grupo 1 = prefixo (contém data/desc/doc)
# grupo 2 = valor da transação (pode ser negativo)
# grupo 3 = saldo corrente (descartado)
#
# O saldo aceita sinal negativo: conta no vermelho é normal e, enquanto o grupo 3
# exigia dígito no início, TODA linha de um extrato com saldo devedor deixava de
# casar. O parser devolvia zero transações, o arquivo caía na camada de IA e a IA
# achatava a linha capturando a coluna de saldo no lugar do valor — foi assim que
# 29 lançamentos da SINCOPEÇAS entraram com o saldo como valor (ver 4ac77cf).
_TX_LINE = re.compile(
    r"^(.+?)\s+"
    r"([-]?\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s+"
    r"([-]?\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*$",
)

# Prefixo de data no início de uma linha
# NOTA: \d{4} deve vir ANTES de \d{2} para evitar que "2026" case como "20" + sobra
_DATE_PREFIX = re.compile(r"^(\d{2}[/\-]\d{2}[/\-](?:\d{4}|\d{2}))\s*")

# Linhas a ignorar sempre.
#
# Cabeçalho não é só ruído: uma linha de texto puro que não casa com _TX_LINE vira
# `pending_desc` e é usada como histórico da PRÓXIMA transação (o formato Bradesco
# depende disso). Sem suprimir o cabeçalho do Sicredi, "Dados referentes ao
# período..." entrava como descrição do primeiro lançamento do extrato.
_SKIP_RE = re.compile(
    r"^(Data\s|Ag[eê]ncia|Extrato|Total\s|Os dados|Pr[oó]ximo|"
    r"^\s*Cr[eé]dito|^\s*D[eé]bito|Saldo \(|REM:\s|"
    r"Associado:|Cooperativa:|Conta Corrente:|Impresso em|Dados referentes)",
    re.IGNORECASE,
)

# Cabeçalho de coluna da tabela ("Data  Descrição  Documento  Valor  Saldo").
# Marca a fronteira entre o cabeçalho do documento e os lançamentos: nada lido
# antes dele pode virar descrição de transação. Sem isso, uma razão social que
# quebrou em duas linhas ("...COM IMPORT DISTR DE AUTOP E" / "MOTOP ROL E AC NO E")
# sobrevivia como `pending_desc` e virava o histórico do primeiro lançamento.
_TABLE_HEADER_RE = re.compile(r"^Data\b.*\bSaldo\b", re.IGNORECASE)

# Limite de texto enviado para a IA (gpt-4o-mini suporta 128k tokens ≈ ~500k chars)
_AI_TEXT_MAX_CHARS = 80_000


def _extrair_linhas_pdfplumber(
    conteudo_bytes: bytes, budget: _PDFBudget
) -> tuple[list[str], int]:
    """Extrai todas as linhas de texto do PDF via pdfplumber.

    Retorna (linhas, total_chars). Se total_chars < 50 o PDF não tem camada de texto.
    """
    all_lines: list[str] = []
    total_chars = 0
    with pdfplumber.open(BytesIO(conteudo_bytes)) as pdf:
        if not pdf.pages:
            raise PDFParseError("PDF sem páginas.")
        if len(pdf.pages) > budget.max_pages:
            raise PDFParseError(
                f"PDF excede o limite de {budget.max_pages} páginas."
            )
        for page in pdf.pages:
            budget.verificar_tempo()
            text = page.extract_text() or ""
            total_chars += len(text)
            all_lines.extend(text.splitlines())
    return all_lines, total_chars


_CID_RE = re.compile(r"\(cid:\d+\)")
_UTEIS = set(" ,.:;/-()R$%*")


def _fracao_legivel(linhas: list[str]) -> float:
    """Quanto do texto extraído é texto de verdade, entre 0 e 1.

    Um PDF pode ter camada de texto e ainda assim não ter texto. Fonte
    embarcada sem `ToUnicode` faz o pdfplumber devolver o CÓDIGO DO GLIFO em
    vez do caractere: saem `(cid:71)(cid:82)` ou caracteres de uso privado, e a
    contagem de caracteres fica ALTA — foi assim que dois extratos do Itaú da JS
    Bertoldo, que renderizam perfeitamente e cujo layout o adaptador já lê,
    nunca chegaram na camada de imagem: 32.960 caracteres de lixo passavam
    folgados no teto de 50.

    Medido nos seis arquivos que motivaram isto, a separação não é sutil:

        legíveis (BB, Caixa, Itaú, Painel)   96,8% a 97,8%
        quebrados (os dois do Itaú)          11,4%  (84% em `(cid:N)`)
    """
    texto = "\n".join(linhas)
    if not texto:
        return 0.0
    sem_cid = _CID_RE.sub("", texto)
    uteis = sum(
        1 for c in sem_cid
        if (c.isalnum() and not (0xE000 <= ord(c) <= 0xF8FF)) or c in _UTEIS
    )
    return uteis / len(texto)


# Abaixo disto, o que veio não é texto — é o glifo sem tradução. O limiar fica
# no meio de um vão de 85 pontos percentuais, então não é número escolhido a
# dedo: qualquer valor entre 0,2 e 0,9 classificaria os seis do mesmo jeito.
_FRACAO_LEGIVEL_MINIMA = 0.5


def _extrair_paginas_palavras(
    conteudo_bytes: bytes, budget: _PDFBudget
) -> list[list[dict]]:
    """Palavras de cada página com suas coordenadas (`x0`, `x1`, `top`).

    O `extract_text` entrega a página já achatada em linhas, na ordem de leitura
    do PDF — que num layout de duas colunas mistura as duas. No Itaú isso põe a
    legenda lateral ("A = agendamento", "B = ações movimentadas…") no meio dos
    lançamentos, e nenhuma regex separa depois o que a extração já juntou.

    Com a coordenada dá para descartar a coluna da legenda pelo `x0` e, mais
    importante, para saber em QUE coluna um valor está — que no Itaú é o que
    diz se ele é entrada ou saída.
    """
    paginas: list[list[dict]] = []
    with pdfplumber.open(BytesIO(conteudo_bytes)) as pdf:
        if not pdf.pages:
            raise PDFParseError("PDF sem páginas.")
        if len(pdf.pages) > budget.max_pages:
            raise PDFParseError(f"PDF excede o limite de {budget.max_pages} páginas.")
        for page in pdf.pages:
            budget.verificar_tempo()
            paginas.append(page.extract_words())
    return paginas


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
    last_date: date | None = None
    pending_desc: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # ── Linhas a ignorar completamente ────────────────────────────────────
        # O cabeçalho da tabela encerra o preâmbulo do documento: qualquer
        # descrição acumulada até aqui é cabeçalho, não lançamento.
        if _TABLE_HEADER_RE.match(line):
            pending_desc = None
            continue
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

        prefix_raw, val_str, saldo_str = m.group(1), m.group(2), m.group(3)

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
        if valor is None or abs(valor) < Decimal("0.001"):
            continue

        # ── Saldo após o lançamento ───────────────────────────────────────────
        # Diferente do valor, saldo ilegível não invalida a transação: é dado de
        # conferência, não o lançamento em si. Zero é saldo legítimo, então só
        # `None` significa ausência.
        saldo_apos = _parse_valor(saldo_str)

        # O fitid NÃO inclui o saldo de propósito: ele é a identidade da
        # transação para deduplicação, e o mesmo lançamento reimportado precisa
        # gerar o mesmo fitid mesmo que o saldo tenha sido lido de outro jeito.
        fitid = _gerar_fitid(last_date, historico, valor, idx)
        transacoes.append(
            TransacaoOFX(
                fitid=fitid,
                data=last_date,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
                saldo_apos=saldo_apos,
                # Posição da linha no arquivo. Desempata lançamentos do mesmo
                # dia: nem saldo nem valor reproduzem a ordem do extrato.
                ordem=idx,
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
        result = json.loads(raw, parse_float=Decimal)
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
        logger.warning("Falha ao parsear JSON da IA: %s (resposta com %d chars)", e, len(raw))
        return []


# ──────────────────────────────────────────────────────────── Camada 2: AI texto

def _parse_por_ai_texto(linhas: list[str], budget: _PDFBudget) -> list[TransacaoOFX]:
    """Camada 2: envia o texto já extraído pelo pdfplumber ao GPT-4o-mini.

    Muito mais barato que Vision (~10× menos custo) porque não processa imagens.
    Usado quando o regex não reconhece o layout do banco (BB, Caixa, Sicoob, etc.).
    """
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        logger.info("AI texto desabilitada para dados financeiros — pulando camada 2")
        return []

    texto = "\n".join(linhas)
    if len(texto) > _AI_TEXT_MAX_CHARS:
        logger.warning(
            "AI texto: texto truncado de %d para %d chars",
            len(texto), _AI_TEXT_MAX_CHARS,
        )
        texto = texto[:_AI_TEXT_MAX_CHARS]

    logger.info("AI texto: enviando %d chars para gpt-4o-mini", len(texto))
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
                {"role": "user", "content": f"Extrato bancário:\n\n{texto}\n\n{_AI_PROMPT}"},
            ],
        )
    except Exception as e:
        logger.warning("AI texto: falha na chamada OpenAI: %s", e)
        return []

    raw = response.choices[0].message.content or ""
    logger.info("AI texto: resposta recebida com %d chars", len(raw))

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


def _ocr_page_with_openai(png_bytes: bytes, budget: _PDFBudget) -> list[dict]:
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        raise PDFParseError(
            "OCR externo desabilitado. Configure consentimento explícito para enviar "
            "dados financeiros ao provedor de IA."
        )
    budget.consumir_chamada_ai()
    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())
    img_b64 = base64.standard_b64encode(png_bytes).decode()
    response = client.chat.completions.create(
        model="gpt-4o",
        timeout=budget.timeout_restante,
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
    logger.info("OCR: resposta recebida com %d chars", len(raw))
    return _parse_ai_response(raw)


def _parse_por_ocr(conteudo_bytes: bytes, budget: _PDFBudget) -> list[TransacaoOFX]:
    if not _FITZ_AVAILABLE:
        raise PDFParseError("PyMuPDF não instalado. Execute: pip install pymupdf")
    doc = fitz.open(stream=conteudo_bytes, filetype="pdf")
    num_pages = doc.page_count
    doc.close()
    if num_pages > budget.max_pages:
        raise PDFParseError(f"PDF excede o limite de {budget.max_pages} páginas.")
    transacoes: list[TransacaoOFX] = []
    idx = 0
    for page_num in range(num_pages):
        budget.verificar_tempo()
        logger.info("OCR: processando página %d/%d", page_num + 1, num_pages)
        try:
            png_bytes = _render_page_to_png(conteudo_bytes, page_num)
            ocr_items = _ocr_page_with_openai(png_bytes, budget)
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
_FIELD_SALDO     = ("saldo", "balance", "saldo_apos", "saldo_atual", "running_balance")


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
                data = datetime.fromisoformat(data_str.replace("/", "-")).date()
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
            valor = Decimal(valor_clean)
        except (TypeError, InvalidOperation):
            valor = _parse_valor(valor_raw)
            if valor is None:
                return None
        dc_hint = str(item.get("tipo", item.get("dc", item.get("type", "")))).upper()
        if dc_hint in ("D", "DB", "DEBITO", "DEBIT", "SAIDA") and valor > 0:
            valor = -valor
        elif dc_hint in ("C", "CR", "CREDITO", "CREDIT", "ENTRADA") and valor < 0:
            valor = abs(valor)
        # O saldo da linha é o que permite conferir a saída da IA: sem ele, a
        # extração por imagem não teria como ser verificada e não deveria ser
        # importada. `None` quando aquela linha não traz saldo impresso.
        saldo_apos = None
        saldo_raw = _get_field(item, _FIELD_SALDO)
        if saldo_raw is not None:
            try:
                saldo_apos = Decimal(
                    saldo_raw.replace("R$", "").replace(" ", "").replace("\xa0", "")
                )
            except (TypeError, InvalidOperation):
                saldo_apos = _parse_valor(saldo_raw)

        fitid = _gerar_fitid(data, historico, valor, idx)
        return TransacaoOFX(
            fitid=fitid,
            data=data,
            valor=valor,
            historico=historico[:200],
            tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
            saldo_apos=saldo_apos,
            ordem=idx,
        )
    except Exception as e:
        logger.warning("IA: item inválido descartado (%s)", type(e).__name__)
        return None


# ──────────────────────────────────────────────── Camada 3: Vision (PDF de imagem)

# ESTE MODELO FOI ESCOLHIDO POR MEDIÇÃO, NÃO POR PREÇO DE TABELA.
#
# Cada linha abaixo é uma rodada COMPLETA dos quatro extratos do Itaú da JS
# BERTOLDO (13 páginas), pelo `parse_pdf`, com este mesmo prompt. O critério é
# ler os quatro; "a cadeia fechou" não basta, e o `luna` explica por quê.
#
#     modelo          R$/página   resultado
#     gpt-5.6-luna      0,010     recusa 3 de 4 — e no que passa PERDE 5
#                                 lançamentos com a cadeia fechando mesmo assim
#     gpt-5.6-terra     0,101     recusa 1 de 4
#     gpt-5.4           0,127     recusa 2 de 4
#     gpt-5.6-sol       0,181     LÊ OS QUATRO (56, 56, 91, 53)
#     gpt-5.5           0,400     lê, com o dobro dos tokens de saída
#     gpt-4o            0,080     funde linhas, `5.969,14-` vira +5969.14,
#                                 troca datas; a 200 dpi continua errando
#
# O `sol` é o mais barato que lê tudo. Baratear mais já foi tentado e não é
# questão de ajustar o prompt: `luna` e `terra` erram de formas diferentes
# (quantidade e layout), e o caso do `luna` mostra que lançamento perdido entre
# duas âncoras pode passar pela conferência. Reserva: `gpt-5.5`.
#
# Sem `temperature=0` (o modelo não aceita), duas execuções independentes dos
# quatro arquivos deram exatamente 56, 56, 91 e 53.
_MODELO_VISION = "gpt-5.6-sol"

# Uma chamada por página. Renderizar em 150 dpi mantém o texto legível sem
# estourar o tamanho da imagem.
_DPI_VISION = 150

# O prompt abaixo é fruto de rodar a Vision nos extratos reais do Itaú da JS
# BERTOLDO e LER a saída. A primeira versão pedia interpretação demais, e cada
# pedaço de interpretação virou um erro:
#
#   - `5.969,14-` voltava como 5969.14: o menos vinha DEPOIS do número, e o
#     prompt só falava do menos antes. Numa conta no vermelho isso inverte
#     TODOS os saldos, e a cadeia quebra no primeiro passo;
#   - o saldo impresso uma vez na última linha do dia era repetido nos três
#     lançamentos daquele dia, criando três âncoras onde havia uma;
#   - o histórico saía como a concatenação do bloco inteiro, igual em todas as
#     linhas dele;
#   - as linhas `SALDO TOTAL DISPONÍVEL DIA`, que o prompt mandava DESCARTAR,
#     eram no layout "lançamentos do período" o único lugar onde o saldo
#     existe. Descartá-las deixava o extrato sem âncora nenhuma.
#
# A correção não é pedir mais cuidado: é parar de pedir julgamento. O modelo
# transcreve o que vê, linha por linha, e QUEM DECIDE onde o saldo se encaixa é
# o código — ver `_ancorar_saldos_do_dia`.
_AI_PROMPT_VISION = """\
Esta é a imagem de UMA página de extrato bancário brasileiro.

Retorne SOMENTE um objeto JSON com estas três chaves:

  "saldos_declarados" → array de objetos
       {"rotulo": texto, "data": "DD/MM/AAAA" ou null, "saldo": número}
       para TODO saldo que a página declara fora das linhas de lançamento:
       "SALDO ANTERIOR", "Saldo anterior", "saldo em DD/MM/AA", "Saldo em C/C",
       "Saldo final", "SALDO ATUAL".

       Copie o RÓTULO como está impresso, e a data quando houver — alguns não
       têm. Não decida qual é abertura e qual é encerramento: só transcreva.

  "transacoes" → array de objetos, um por LINHA de lançamento, na ordem em que
       aparecem na página (de cima para baixo, sem reordenar), cada um com:

       "data"      → "DD/MM/AAAA"
       "historico" → a descrição DAQUELA LINHA, só dela
       "valor"     → decimal com ponto, negativo para saída, positivo para
                     entrada
       "saldo"     → o saldo impresso NAQUELA MESMA LINHA, ou null

  "saldos_do_dia" → array de objetos {"data": "DD/MM/AAAA", "saldo": número},
       um para cada linha que declara o saldo DA CONTA no fim de um dia
       ("SALDO DIA", "Saldo do dia", "SALDO TOTAL DISPONÍVEL DIA" e
       semelhantes). Elas NÃO entram em "transacoes" — o saldo delas é
       aproveitado separadamente.

Regras que não podem ser quebradas:

- O SINAL PODE VIR ANTES OU DEPOIS DO NÚMERO. "-1.234,56" e "1.234,56-" são o
  MESMO valor negativo. Isso vale tanto para "valor" quanto para "saldo" e para
  "saldo_anterior": um saldo com o menos no fim é NEGATIVO, a conta está no
  vermelho. Um "D" à direita também indica negativo; "C" indica positivo.
- UM SALDO PERTENCE A UMA LINHA SÓ. Preencha "saldo" apenas na linha em que ele
  está impresso. Quando o extrato imprime o saldo uma vez por dia ou por grupo,
  as outras linhas daquele dia levam null. NUNCA repita o mesmo saldo em várias
  linhas — isso cria âncoras falsas e faz a importação ser recusada.
- UMA LINHA, UM HISTÓRICO. Não junte a descrição de linhas diferentes num
  histórico só, mesmo quando elas pertencem ao mesmo dia ou ao mesmo bloco.
- SÓ É LANÇAMENTO O QUE ESTÁ NA TABELA DE MOVIMENTAÇÃO DA CONTA CORRENTE — a
  que tem colunas de data, descrição, valor e saldo. A linha "Saldo final"
  ENCERRA essa tabela: nada abaixo dela é lançamento.

  Estes quadros trazem datas e valores e NÃO são lançamentos:
  "Aviso de débito(s)" (é o aviso de uma tarifa que já foi lançada na tabela —
  incluí-la cobra a mesma tarifa duas vezes), "totalizador de aplicações
  automáticas", "movimentação - aplicações/resgates antecipados", resumos de
  tarifas, limites de cheque especial e notas explicativas. Se a página tiver
  só quadros desses, devolva as listas vazias.
- SALDO DE OUTRA CONTA NÃO É NADA DISTO. Linhas como "SALDO APLIC AUT MAIS",
  "SALDO APLICAÇÃO" ou saldo de poupança mostram quanto há na aplicação
  automática, que é outra conta: não são lançamento e não são saldo do dia da
  conta corrente. IGNORE essas linhas por completo — não crie lançamento algum
  a partir delas, nem com nome inventado como "Aplicação realizada".
- Transcreva os números EXATAMENTE como impressos. "1.234,56" vira 1234.56.
- Não invente lançamento, não junte dois numa linha só e não pule nenhum: os
  saldos são conferidos um contra o outro depois, e qualquer linha faltando
  faz a importação inteira ser recusada.
- Se a página não tiver lançamento nenhum (capa, legenda, rodapé), devolva as
  listas vazias — mas a CAPA costuma declarar os saldos de abertura e de
  encerramento, e esses vão em "saldos_declarados" mesmo sem lançamento algum."""


def _parse_por_ai_vision(
    conteudo_bytes: bytes, budget: _PDFBudget
) -> tuple[list[TransacaoOFX], Decimal | None, Decimal | None]:
    """Camada 3: lê um PDF SEM camada de texto renderizando cada página.

    Existe porque quatro extratos do escritório são PDF de imagem — não são
    escaneados, são exportações vetoriais do internet banking, com texto
    perfeitamente legível e nenhuma camada de texto para o pdfplumber.

    **O que torna isto seguro é a cadeia de saldos.** Os quatro trazem saldo,
    e o prompt pede o saldo de cada linha junto do valor. Depois disso a saída
    da IA passa exatamente pela mesma conferência de um adaptador determinístico
    (`_validar_blocos`): se ela alucinar um valor, pular uma linha ou trocar um
    sinal, o saldo deixa de caminhar e a importação é recusada. Sem essa
    conferência esta camada não deveria existir — foi por isso que ela ficou
    documentada e não implementada até agora.

    Devolve (transações, saldo_anterior).
    """
    settings = get_settings()
    if not settings.openai_enabled or not settings.allow_financial_data_to_openai:
        logger.info("Vision desabilitada para dados financeiros — pulando camada 3")
        return [], None, None
    if not _FITZ_AVAILABLE:
        logger.warning("Vision indisponível: PyMuPDF não instalado")
        return [], None, None

    doc = fitz.open(stream=conteudo_bytes, filetype="pdf")
    total_paginas = len(doc)
    doc.close()

    if total_paginas > budget.ai_calls_restantes:
        raise PDFParseError(
            f"O PDF tem {total_paginas} páginas sem camada de texto e a leitura "
            f"por imagem processa no máximo {budget.ai_calls_restantes}. "
            "Exporte o extrato em um período menor, ou peça ao banco o arquivo "
            "com texto selecionável."
        )

    # Uma chamada por página, todas debitadas de uma vez: com as páginas em voo
    # simultâneo não existe mais uma ordem em que debitar uma a uma.
    budget.consumir_chamada_ai(total_paginas)
    # O relógio desta camada é outro. Ver `pdf_vision_timeout_seconds`: enquanto
    # ela dividia o prazo com o caminho determinístico, estourava na terceira
    # página e o extrato de 12 que motivou `pdf_max_ai_calls: 30` nunca chegava
    # ao fim.
    budget.estender_deadline(settings.pdf_vision_timeout_seconds)

    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())

    def ler_pagina(numero: int) -> dict:
        png = _render_page_to_png(conteudo_bytes, numero, dpi=_DPI_VISION)
        img_b64 = base64.b64encode(png).decode()
        response = client.chat.completions.create(
            model=_MODELO_VISION,
            timeout=budget.timeout_chamada(settings.pdf_vision_call_timeout_seconds),
            # `max_completion_tokens`, e NÃO `max_tokens`: este modelo recusa o
            # parâmetro antigo com 400. Também recusa `temperature=0` — só
            # aceita o padrão. Trocar o nome do modelo sem trocar os dois teria
            # feito toda chamada falhar.
            #
            # Uma página densa do Itaú (40 lançamentos com CNPJ no histórico)
            # mede ~1.600 tokens de saída. O teto dá folga para históricos mais
            # longos; estourar significa perder a página inteira, e por isso
            # `finish_reason == "length"` virou recusa explícita abaixo.
            max_completion_tokens=8192,
            messages=[
                {"role": "system", "content": _AI_SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{img_b64}",
                        "detail": "high",
                    }},
                    {"type": "text", "text": _AI_PROMPT_VISION},
                ]},
            ],
        )
        escolha = response.choices[0]
        # Resposta cortada no meio é a falha mais traiçoeira desta camada: o
        # JSON fica inválido, a página vira uma lista vazia com um aviso no log,
        # e o arquivo é recusado por "extração não verificável" — culpando o
        # extrato por um teto nosso. Medido, uma página de 40 lançamentos gasta
        # ~2.100 tokens; em 4.096 ela cabia por pouco e estourava assim que os
        # históricos ficavam mais longos.
        if escolha.finish_reason == "length":
            raise PDFParseError(
                f"A leitura por imagem da página {numero + 1} veio incompleta: "
                "a página tem lançamentos demais para uma resposta só. Exporte "
                "o extrato em um período menor."
            )
        return _parse_ai_response_vision(escolha.message.content or "")

    # Páginas são independentes — a ordem de leitura não importa porque ela é
    # restaurada pelo número da página logo abaixo, e é a ordem restaurada que
    # a cadeia de saldos confere.
    trabalhadores = max(1, min(settings.pdf_vision_concurrency, total_paginas))
    paginas: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=trabalhadores) as pool:
        futuros = {pool.submit(ler_pagina, n): n for n in range(total_paginas)}
        for futuro in as_completed(futuros):
            numero = futuros[futuro]
            try:
                paginas[numero] = futuro.result()
            except Exception as e:
                for pendente in futuros:
                    pendente.cancel()
                # Página perdida quebra a cadeia de saldos de qualquer jeito, e
                # a recusa que vinha depois falava de "extração não
                # verificável" — mandava o contador procurar problema no
                # extrato quando o problema tinha sido a chamada. Falhar aqui,
                # dizendo o que falhou.
                if isinstance(e, PDFParseError):
                    raise
                raise PDFParseError(
                    f"A leitura por imagem falhou na página {numero + 1} "
                    f"({type(e).__name__}). Nenhum lançamento foi importado."
                ) from e

    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    saldos_do_dia: list[tuple[date, Decimal]] = []
    declarados: list[tuple[str, date | None, Decimal]] = []

    for numero in range(total_paginas):
        conteudo = paginas[numero]
        if saldo_anterior is None and conteudo.get("saldo_anterior") is not None:
            saldo_anterior = _parse_valor(str(conteudo["saldo_anterior"]))

        for item in conteudo.get("transacoes", []):
            transacao = _transacao_from_ai(item, idx)
            if transacao:
                transacoes.append(transacao)
                idx += 1

        for item in conteudo.get("saldos_do_dia", []):
            if not isinstance(item, dict):
                continue
            data_lida = _parse_data(str(item.get("data", "")))
            saldo = _parse_valor(str(item.get("saldo", "")))
            if data_lida is not None and saldo is not None:
                saldos_do_dia.append((data_lida, saldo))

        # Aqui a data pode faltar: `Saldo em C/C` vem sem ela, e é justamente
        # esse que fecha a conta corrente.
        for item in conteudo.get("saldos_declarados", []):
            if not isinstance(item, dict):
                continue
            saldo = _parse_valor(str(item.get("saldo", "")))
            if saldo is None:
                continue
            declarados.append((
                str(item.get("rotulo") or ""),
                _parse_data(str(item.get("data") or "")),
                saldo,
            ))

    # A ordem cronológica precisa vir ANTES de encaixar os saldos do dia: o
    # saldo de um dia é o que vale depois do último lançamento dele, e "último"
    # só existe depois de ordenar. O extrato do Itaú sai do mais recente para o
    # mais antigo, então na ordem da página o último é o primeiro.
    transacoes = _ancorar_saldos_do_dia(
        ordenar_do_mais_antigo(transacoes), saldos_do_dia
    )
    abertura, saldo_final = _pontas_declaradas(declarados, transacoes)
    if saldo_anterior is None:
        saldo_anterior = abertura

    logger.info(
        "Vision: %d transações em %d páginas (abertura=%s, fecho=%s, "
        "%d saldos de dia)",
        len(transacoes), total_paginas, saldo_anterior, saldo_final,
        len(saldos_do_dia),
    )
    return transacoes, saldo_anterior, saldo_final


def _parse_ai_response_vision(raw: str) -> dict:
    """Aceita o objeto com `saldo_anterior`/`transacoes`, ou só a lista."""
    vazio = {"saldo_anterior": None, "transacoes": [], "saldos_do_dia": []}
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```\s*$", "", raw).strip()
    try:
        resultado = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as e:
        logger.warning("Vision: JSON inválido (%s)", e)
        return vazio
    if isinstance(resultado, list):
        return {**vazio, "transacoes": resultado}
    if isinstance(resultado, dict):
        return {
            "saldo_anterior": resultado.get("saldo_anterior"),
            "transacoes": resultado.get("transacoes") or resultado.get("transactions") or [],
            "saldos_do_dia": resultado.get("saldos_do_dia") or [],
            "saldos_declarados": resultado.get("saldos_declarados") or [],
        }
    return vazio


_ROTULO_ABERTURA = re.compile(r"saldo\s+anterior|saldo\s+inicial", re.IGNORECASE)
_ROTULO_FECHO_DA_CONTA = re.compile(r"saldo\s+em\s+c\s*/?\s*c\b", re.IGNORECASE)


def _pontas_declaradas(
    declarados: list[tuple[str, date | None, Decimal]],
    transacoes: list[TransacaoOFX],
) -> tuple[Decimal | None, Decimal | None]:
    """Escolhe, entre os saldos que a página declara, quais fecham a cadeia.

    **O rótulo decide, não o valor nem a posição.** O extrato do Itaú declara
    três saldos e só um deles fecha a conta corrente:

        capa      saldo em 25/02/26   5.969,14-   ┐ conta corrente MAIS a
        capa      saldo em 31/03/26  17.138,32    ┘ aplicação automática
        rodapé    Saldo em C/C            1,00      só a conta corrente

    Os 17.138,32 da capa são 1,00 de conta corrente com 17.137,32 aplicados.
    Usá-los para fechar a cadeia acusa uma diferença de 17.137,32 que não é
    lançamento nenhum — é o dinheiro que está na aplicação. É a mesma regra que
    o adaptador determinístico do Itaú já segue.

    Por isso o fecho SÓ é aceito com o rótulo da conta corrente. Sem ele, o
    bloco fica sem cauda coberta e a conferência recusa — o que é melhor do que
    fechar com o número errado.
    """
    if not declarados or not transacoes:
        return None, None

    abertura = next(
        (saldo for rotulo, _, saldo in declarados if _ROTULO_ABERTURA.search(rotulo)),
        None,
    )
    if abertura is None:
        # Sem rótulo de abertura, vale um saldo datado antes do primeiro
        # lançamento — na primeira data do extrato a aplicação costuma ser zero,
        # e é a única ponta disponível.
        anteriores = sorted(
            (d for d in declarados if d[1] is not None and d[1] <= transacoes[0].data),
            key=lambda d: d[1],
        )
        abertura = anteriores[-1][2] if anteriores else None

    fecho = next(
        (
            saldo
            for rotulo, _, saldo in declarados
            if _ROTULO_FECHO_DA_CONTA.search(rotulo)
        ),
        None,
    )
    return abertura, fecho


def _ancorar_saldos_do_dia(
    transacoes: list[TransacaoOFX], saldos_do_dia: list[tuple[date, Decimal]]
) -> list[TransacaoOFX]:
    """Prende o saldo declarado de cada dia ao ÚLTIMO lançamento daquele dia.

    Há layouts — o "lançamentos do período" do Itaú é um — em que o saldo NÃO
    aparece na linha do lançamento: existe só numa linha `SALDO TOTAL DISPONÍVEL
    DIA` por dia. Sem aproveitá-la o extrato não tem âncora nenhuma e é recusado
    por inconferível.

    Encaixar isso é trabalho de código, não de prompt. Quando o modelo tentava,
    ele grudava o saldo do dia no lançamento vizinho de cima — e como o extrato
    sai do mais recente para o mais antigo, o saldo de um dia ia parar no dia
    seguinte. Aqui a lista já está em ordem cronológica, e o saldo do dia é, por
    definição, o que vale DEPOIS do último lançamento dele.

    Só preenche onde está vazio: saldo impresso na própria linha manda mais.
    """
    if not saldos_do_dia:
        return transacoes

    por_data = dict(saldos_do_dia)
    ultimo_indice: dict[date, int] = {}
    for indice, transacao in enumerate(transacoes):
        ultimo_indice[transacao.data] = indice

    resultado = list(transacoes)
    for data_do_saldo, saldo in por_data.items():
        indice = ultimo_indice.get(data_do_saldo)
        if indice is None or resultado[indice].saldo_apos is not None:
            continue
        resultado[indice] = replace(resultado[indice], saldo_apos=saldo)
    return resultado


# ──────────────────────────────────────────────────────────── entrypoint

def _recusa_de_adaptador_vazio(adaptador, linhas: list[str]) -> str:
    """Diz que o leitor do banco foi acionado e não achou a tabela.

    São dois casos com conselhos OPOSTOS, e `reconhece` os separa:

    - a assinatura do layout casou → é mesmo este banco, e a tabela é que mudou.
      Não há nada que o contador possa fazer no arquivo; o conserto é aqui.
    - não casou → o leitor veio da sigla da agência cadastrada, e o arquivo não
      se parece com esse banco. Aí a pergunta certa é se o extrato foi baixado
      da conta certa, ou se a agência está com o banco errado no cadastro.

    Sem essa frase, os dois saíam como "não foi possível obter uma extração com
    saldos verificáveis" — que manda procurar defeito no extrato nos dois casos.
    """
    nome = adaptador.__name__.rsplit(".", 1)[-1].upper()
    if adaptador.reconhece(linhas):
        return (
            f"O leitor do {nome} reconheceu o extrato, mas não encontrou a "
            "tabela de lançamentos — provavelmente é um layout novo deste "
            "banco. Nenhum lançamento foi importado; envie este arquivo ao "
            "suporte para que o leitor seja ajustado."
        )
    return (
        f"O leitor do {nome} foi usado porque é o banco cadastrado na agência, "
        "mas o arquivo não tem o layout dele e nenhum lançamento foi "
        "importado. Confira se o extrato é desta conta e se a agência está com "
        "o banco correto no cadastro."
    )


def parse_pdf(conteudo_bytes: bytes, banco_sigla: str | None = None) -> list[TransacaoOFX]:
    """Extrai transações de um PDF de extrato bancário.

    Fluxo em camadas (para-na-primeira-que-funcionar):

      Camada 0 — adaptador do banco
        Escolhido por `banco_sigla` (que vem da agência cadastrada) ou pela
        assinatura do layout. Determinístico e conferido pela cadeia de saldos
        de cada bloco. É o único caminho que consegue reconstruir histórico
        partido em várias linhas (Bradesco) ou data em cabeçalho (Inter).

      Camada 1 — pdfplumber + regex genérico
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

    settings = get_settings()
    budget = _PDFBudget(
        deadline=time.monotonic() + settings.pdf_parse_timeout_seconds,
        max_pages=settings.pdf_max_pages,
        ai_calls_restantes=settings.pdf_max_ai_calls,
    )

    # Adaptador que reconheceu o banco e não extraiu nada. Guardado aqui porque
    # quem sabe disso é a camada 0, e quem precisa contar é a recusa no fim.
    adaptador_vazio = None
    linhas: list[str] = []

    # ── Camadas 1 e 2: requerem pdfplumber ───────────────────────────────────
    if _PDFPLUMBER_AVAILABLE:
        try:
            linhas, total_chars = _extrair_linhas_pdfplumber(conteudo_bytes, budget)
        except PDFParseError:
            raise
        except Exception as e:
            logger.warning("pdfplumber: falha na extração de texto: %s", e)
            linhas, total_chars = [], 0

        legibilidade = _fracao_legivel(linhas)
        if total_chars >= 50 and legibilidade >= _FRACAO_LEGIVEL_MINIMA:
            logger.info("pdfplumber: %d chars extraídos", total_chars)

            referencia_ano = datetime.now(UTC).year

            # ── Camada 0: adaptador do banco ─────────────────────────────────
            adaptador = bancos.escolher(banco_sigla, linhas)
            if adaptador is not None:
                if hasattr(adaptador, "extrair_de_palavras"):
                    # Layout de duas colunas: precisa da coordenada, não do
                    # texto já achatado. Custa uma segunda passada no PDF.
                    paginas = _extrair_paginas_palavras(conteudo_bytes, budget)
                    blocos = adaptador.extrair_de_palavras(paginas, referencia_ano)
                else:
                    blocos = adaptador.extrair(linhas, referencia_ano)
                transacoes = [t for bloco in blocos for t in bloco.transacoes]
                if transacoes:
                    if not _validar_blocos(blocos, Decimal("0.05")):
                        # Adaptador sem saldo por lançamento (Itaú imprime só em
                        # algumas linhas): cai na conferência por totais.
                        _validar_completude(linhas, transacoes)
                    # `ordem` é a posição no ARQUIVO, não no bloco.
                    transacoes = [
                        replace(t, ordem=i) for i, t in enumerate(transacoes)
                    ]
                    logger.info(
                        "PDF parser: %d transações via adaptador %s (camada 0)",
                        len(transacoes), adaptador.__name__.rsplit(".", 1)[-1],
                    )
                    return transacoes
                # Guardado para a recusa lá embaixo. Adaptador certo produzindo
                # ZERO lançamentos é a pior falha que este módulo tem: nada
                # aponta para a causa, e a mensagem final falava de "extração
                # não verificável" — que manda procurar problema no extrato
                # quando o problema é a tabela que o leitor não achou. Três dos
                # cinco defeitos dos extratos da JS BERTOLDO tinham essa cara.
                adaptador_vazio = adaptador
                logger.info(
                    "adaptador %s reconheceu o layout mas não extraiu lançamentos "
                    "— seguindo para o parser genérico",
                    adaptador.__name__.rsplit(".", 1)[-1],
                )

            # ── Camada 1: regex genérico ─────────────────────────────────────
            transacoes = _parse_linhas_multipagina(linhas, referencia_ano)
            if transacoes:
                _validar_completude(linhas, transacoes)
                logger.info(
                    "PDF parser: %d transações via pdfplumber regex (camada 1)",
                    len(transacoes),
                )
                return transacoes

            logger.info(
                "pdfplumber regex: 0 transações — banco não mapeado, "
                "tentando AI texto (camada 2)"
            )

            # ── Camada 2: AI texto ────────────────────────────────────────────
            transacoes = _parse_por_ai_texto(linhas, budget)
            if transacoes:
                _validar_completude(linhas, transacoes)
                logger.info("PDF parser: %d transações via AI texto (camada 2)", len(transacoes))
                return transacoes

            logger.info("AI texto: 0 transações extraídas")
        else:
            # ── Camada 3: Vision ──────────────────────────────────────────────
            # PDF sem camada de texto. Os quatro do escritório que caem aqui não
            # são escaneados: são exportações do internet banking em que o texto
            # foi desenhado, não escrito. Todos trazem saldo, e é isso que
            # permite conferir a saída da IA como se fosse a de um adaptador.
            logger.info(
                "pdfplumber: sem texto aproveitável (%d chars, %.0f%% legível) "
                "— tentando leitura por imagem (camada 3)",
                total_chars, legibilidade * 100,
            )
            transacoes, saldo_anterior, saldo_final = _parse_por_ai_vision(
                conteudo_bytes, budget
            )
            if transacoes:
                cronologicas = ordenar_do_mais_antigo(transacoes)
                bloco = Bloco(
                    transacoes=cronologicas,
                    saldo_anterior=saldo_anterior,
                    saldo_final=saldo_final,
                )
                # A conferência é a MESMA de um adaptador determinístico. Se a
                # IA pulou uma linha ou trocou um sinal, o saldo não caminha e
                # nada é importado.
                if not _validar_blocos([bloco], Decimal("0.05")):
                    raise PDFParseError(
                        "A leitura por imagem não pôde ser conferida: o extrato "
                        "não trouxe saldo suficiente para verificar os "
                        "lançamentos, e importar sem conferência não é seguro."
                    )
                transacoes = [
                    replace(t, ordem=i) for i, t in enumerate(cronologicas)
                ]
                logger.info(
                    "PDF parser: %d transações via Vision (camada 3)", len(transacoes)
                )
                return transacoes

    if adaptador_vazio is not None:
        raise PDFParseError(_recusa_de_adaptador_vazio(adaptador_vazio, linhas))

    raise PDFParseError(
        "Não foi possível obter uma extração com saldos/totais verificáveis; "
        "nenhum lançamento foi importado."
    )


_VALOR_DECLARADO_RE = re.compile(
    r"(?P<valor>-?\(?\d[\d.]*,\d{2}\)?)(?:\s*(?P<dc>[DC]))?\s*$",
    re.IGNORECASE,
)


def _valor_declarado(linha: str) -> Decimal | None:
    match = _VALOR_DECLARADO_RE.search(linha)
    if not match:
        return None
    valor = _parse_valor(match.group("valor"))
    if valor is None:
        return None
    decimal = valor
    dc = (match.group("dc") or "").upper()
    if dc == "D":
        decimal = -abs(decimal)
    elif dc == "C":
        decimal = abs(decimal)
    return decimal


def _validar_cadeia_de_saldos(
    transacoes: list[TransacaoOFX], tolerancia: Decimal
) -> bool:
    """Confere que o saldo caminha de um lançamento para o outro.

    Num extrato, `saldo[n] - saldo[n-1]` é exatamente `valor[n]`. Essa igualdade
    é a verificação de completude mais forte que existe, e não depende de o banco
    imprimir "SALDO ANTERIOR" ou "TOTAL DÉBITOS": lançamento faltando faz o saldo
    pular mais que o valor, e valor trocado quebra o elo.

    É o que faltava. A conferência por soma total aceita, sem perceber, uma
    extração que perdeu lançamentos e trocou valores de modo que os erros se
    cancelam — foi assim que a SINCOPEÇAS ficou com 34 valores errados e ~144
    lançamentos ausentes em fev–mai/2026 sem que nada reclamasse. Também é a
    única validação que funciona no Sicredi, cujo extrato não traz nenhuma das
    linhas de resumo que as outras estratégias procuram.

    Retorna True se a cadeia foi conferida (isto é, se havia saldo para tanto).
    Só as origens que informam saldo por lançamento entram aqui: OFX e as
    camadas de IA passam direto, sem alegar validação que não fizeram.
    """
    com_saldo = [t for t in transacoes if t.saldo_apos is not None]
    if len(com_saldo) < 2 or len(com_saldo) != len(transacoes):
        # Cadeia parcial não prova nada sobre os buracos — não alegamos validação.
        return False

    for anterior, atual in zip(transacoes, transacoes[1:]):
        movimento = atual.saldo_apos - anterior.saldo_apos  # type: ignore[operator]
        if abs(movimento - atual.valor) > tolerancia:
            raise PDFParseError(
                "Extração incompleta: o saldo do extrato não caminha com os "
                f"lançamentos. Depois de {_fmt_reais(anterior.saldo_apos)} o saldo "  # type: ignore[arg-type]
                f"vai para {_fmt_reais(atual.saldo_apos)}, uma variação de "  # type: ignore[arg-type]
                f"{_fmt_reais(movimento)}, mas o lançamento "
                f"'{atual.historico[:60]}' é de {_fmt_reais(atual.valor)} — "
                "há lançamento faltando ou com valor errado entre os dois."
            )
    return True


def _validar_blocos(blocos: list[Bloco], tolerancia: Decimal) -> bool:
    """Confere a cadeia de saldos de cada bloco, por segmentos.

    A cadeia só vale DENTRO de um bloco: entre um e outro o saldo reinicia por
    construção, e conferir a emenda acusaria um salto que não é erro. Foi o que
    reprovava o Bradesco inteiro — ele emite "Extrato" e "Últimos Lançamentos"
    no mesmo arquivo, cada um com seu `SALDO ANTERIOR`.

    A conferência é por SEGMENTO, não lançamento a lançamento: nem todo banco
    imprime saldo em toda linha. O Itaú imprime em 17 das 91, e entre duas
    linhas com saldo a soma dos valores no meio tem de dar exatamente a
    diferença. Isso conserva a força da verificação (lançamento perdido no meio
    do segmento desloca a soma) e deixa de exigir um saldo por linha, que era o
    que tornava o Itaú inverificável.

    As duas pontas vêm dos saldos impressos do bloco: `saldo_anterior` fecha o
    primeiro segmento e `saldo_final` fecha a cauda. Sem a cauda coberta o bloco
    é recusado — e é preciso: foi ali, nas seis linhas depois do último saldo,
    que uma linha de rodapé do Itaú entrou como crédito de R$ 19.070,30.

    Retorna True se todos os blocos puderam ser conferidos de ponta a ponta.
    """
    if not blocos:
        return False

    for bloco in blocos:
        if not bloco.transacoes:
            return False

        ancora = bloco.saldo_anterior
        acumulado = Decimal("0")
        pendentes = 0
        conferidos = 0
        primeiro_segmento = True

        for transacao in bloco.transacoes:
            acumulado += transacao.valor
            pendentes += 1
            if transacao.saldo_apos is None:
                continue
            if ancora is not None:
                movimento = transacao.saldo_apos - ancora
                if abs(movimento - acumulado) > tolerancia:
                    onde = (
                        "há lançamento faltando no início do extrato."
                        if primeiro_segmento and bloco.saldo_anterior is not None
                        else "há lançamento faltando ou com valor errado entre os dois."
                    )
                    quantos = (
                        f"{pendentes} lançamentos somam"
                        if pendentes > 1
                        else "o lançamento"
                    )
                    raise PDFParseError(
                        "Extração incompleta: o saldo do extrato não caminha com "
                        f"os lançamentos. Depois de {_fmt_reais(ancora)} o saldo "
                        f"vai para {_fmt_reais(transacao.saldo_apos)}, uma variação "
                        f"de {_fmt_reais(movimento)}, mas {quantos} "
                        f"{_fmt_reais(acumulado)} até "
                        f"'{transacao.historico[:60]}' — {onde}"
                    )
                conferidos += 1
                primeiro_segmento = False
            ancora = transacao.saldo_apos
            acumulado = Decimal("0")
            pendentes = 0

        if bloco.saldo_final is not None and ancora is not None:
            movimento = bloco.saldo_final - ancora
            if abs(movimento - acumulado) > tolerancia:
                raise PDFParseError(
                    "Extração incompleta: os últimos lançamentos não fecham com o "
                    f"saldo final impresso. De {_fmt_reais(ancora)} para "
                    f"{_fmt_reais(bloco.saldo_final)} há uma variação de "
                    f"{_fmt_reais(movimento)}, mas os lançamentos do trecho somam "
                    f"{_fmt_reais(acumulado)} — há lançamento sobrando ou faltando "
                    "no fim do extrato."
                )
            conferidos += 1
            pendentes = 0

        if conferidos == 0 or pendentes > 0:
            # Cadeia inexistente, ou uma cauda que ninguém conferiu: não
            # alegamos validação e deixamos a conferência por totais decidir.
            return False

    return True


def _fmt_reais(valor: Decimal) -> str:
    """Formata no padrão brasileiro — a mensagem é lida por contador."""
    inteiro, _, centavos = f"{abs(valor):.2f}".partition(".")
    milhar = f"{int(inteiro):,}".replace(",", ".")
    return f"{'-' if valor < 0 else ''}R$ {milhar},{centavos}"


def _validar_completude(linhas: list[str], transacoes: list[TransacaoOFX]) -> None:
    saldos_iniciais: list[Decimal] = []
    saldos_finais: list[Decimal] = []
    total_debitos: Decimal | None = None
    total_creditos: Decimal | None = None
    for linha in linhas:
        normalizada = linha.upper()
        valor = _valor_declarado(linha)
        if valor is None:
            continue
        if "SALDO ANTERIOR" in normalizada or "SALDO INICIAL" in normalizada:
            saldos_iniciais.append(valor)
        elif "SALDO FINAL" in normalizada or "SALDO ATUAL" in normalizada:
            saldos_finais.append(valor)
        elif "TOTAL" in normalizada and ("DÉBIT" in normalizada or "DEBIT" in normalizada):
            total_debitos = abs(valor)
        elif "TOTAL" in normalizada and ("CRÉDIT" in normalizada or "CREDIT" in normalizada):
            total_creditos = abs(valor)

    valores = [t.valor for t in transacoes]
    tolerancia = Decimal("0.05")
    validou = _validar_cadeia_de_saldos(transacoes, tolerancia)
    if saldos_iniciais and saldos_finais:
        diferenca = saldos_finais[-1] - saldos_iniciais[0]
        if abs(sum(valores, Decimal("0")) - diferenca) > tolerancia:
            raise PDFParseError(
                "Extração incompleta: lançamentos não reconciliam saldo inicial e final."
            )
        validou = True
    if total_debitos is not None and total_creditos is not None:
        debitos = sum((-v for v in valores if v < 0), Decimal("0"))
        creditos = sum((v for v in valores if v > 0), Decimal("0"))
        if abs(debitos - total_debitos) > tolerancia or abs(creditos - total_creditos) > tolerancia:
            raise PDFParseError(
                "Extração incompleta: lançamentos não reconciliam os totais declarados."
            )
        validou = True
    if not validou:
        raise PDFParseError(
            "Não foi possível validar a completude: saldos ou totais declarados não encontrados."
        )
