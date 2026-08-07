"""
Módulo de IA para o parser de Razão de Fornecedores.

Responsabilidades:
1. parsear_bloco_fornecedor_ia() — extração + conciliação de um bloco via gpt-4o
2. parsear_bloco_fornecedor_ia_visao() — mesmo pipeline, mas via Vision (imagens)
3. classificar_lancamentos_incertos() — fallback para lançamentos incertos
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

TIPOS_INCERTOS = {"DEBITO", "CREDITO", "OUTRO"}


class VisionBatchError(RuntimeError):
    """A extração Vision não produziu um batch completo e confiável."""


# ---------------------------------------------------------------------------
# Prompt de EXTRAÇÃO — usado pelo gpt-4o-mini no fallback de texto.
# ---------------------------------------------------------------------------
PARSE_SYSTEM_PROMPT = """Você é um parser especializado em Razão de Fornecedores (contabilidade brasileira).

Receberá o texto bruto de um bloco de fornecedor extraído de PDF. PDFs diferentes produzem formatos distintos — você deve reconhecer e tratar todos eles.

FORMATOS POSSÍVEIS:

Formato 1 — linha única (histórico junto com data):
  "10/01/2025 4 COMPRAS CONFORME NF. Nº 21100 55 4.524,08 8.654,11C"
  → data=10/01/2025, lote=4, historico="COMPRAS CONFORME NF. Nº 21100", conta_partida=55, valor_credito=4524.08, saldo_apos=8654.11, saldo_tipo=C

Formato 2 — histórico ANTES da linha de data (muito comum em PDFs de tabela):
  "COMPRA CONFORME NF NÚMERO 1263290 DE 13.101,10C"   ← histórico + saldo (sem data — IGNORE o valor aqui)
  "CASSOL MATERIAIS DE CONSTRUCAO LTDA"               ← ruído — IGNORE
  "26/04/2024 7459 902 13.101,10"                     ← data + lote + CPC + valor real
  → data=26/04/2024, lote=7459, historico="COMPRA CONFORME NF NÚMERO 1263290 DE", conta_partida=902, valor_credito=13101.10, saldo_apos=13101.10, saldo_tipo=C

Formato 3 — pagamento com histórico intercalado antes do lote:
  "09/05/2024 SISPAG BOLETO BANCO 341 7715 SISPAG BOLETO BANCO 341 552 4.150,00 SISPAG BOLETO BANCO 341 35.166,01C"
  → data=09/05/2024, lote=7715, historico="SISPAG BOLETO BANCO 341", conta_partida=552, valor_debito=4150.00, saldo_apos=35166.01, saldo_tipo=C

Formato 4 — histórico repetido N vezes na mesma linha (artefato de PDF com layout multi-coluna):
  "20/05/2024 VLR REF PGTO FGTS 04/2024 1167 VLR REF PGTO FGTS 04/2024 VLR REF PGTO FGTS 04/2024 VLR REF PGTO FGTS 04/2024 9 1.118,34 VLR REF PGTO FGTS 04/2024 4.095,27C"
  → data=20/05/2024, lote=1167, historico="VLR REF PGTO FGTS 04/2024" (use apenas UMA ocorrência), conta_partida=9, valor_debito=1118.34, saldo_apos=4095.27, saldo_tipo=C
  IDENTIFICAÇÃO: a mesma frase de texto aparece 3 ou mais vezes intercalada com números — é sempre o histórico repetido pelo layout do PDF.

Formato 5 — histórico fragmentado em 2 linhas antes da data (histórico quebrado pela largura da coluna):
  "VALOR REFERENTE FGTS S/FOLHA DE 5.154,41C"   ← 1ª parte do histórico + saldo (SEM data — ignore o valor numérico aqui)
  "PAGAMENTO - 05/2024"                          ← 2ª parte do histórico (sem data, sem valor)
  "31/05/2024 370 1.059,14"                      ← data + lote + valor REAL do lançamento
  → data=31/05/2024, lote=370, historico="VALOR REFERENTE FGTS S/FOLHA DE PAGAMENTO - 05/2024", valor_credito=1059.14, saldo_apos=5154.41, saldo_tipo=C
  IDENTIFICAÇÃO: linha sem data que termina com valor+C/D seguida de outra linha sem data e sem valor — ambas são o histórico. O valor real e a data sempre estão na linha que COMEÇA com DD/MM/YYYY.

Formato 6 — COLUNAS ALINHADAS (o texto preserva o espaçamento original do PDF):
  Quando a primeira linha do bloco é o header da tabela, a POSIÇÃO HORIZONTAL de cada
  número diz a que coluna ele pertence. Conte os caracteres.

     Data         Lote  Histórico                          Cta.C.Part.       Débito         Crédito         Saldo-Exercício
  28/02/2026      11190 VALOR A RECOLHER DE IPI DO PERÍODO         425                      2.758,20        2.758,20C
  28/02/2026      11192 VALOR A RECUPERAR DE IPI DO PERÍODO         29       2.758,20                            0,00

  → 1ª: valor 2.758,20 alinhado sob "Crédito"  → valor_credito=2758.20, valor_debito=0, saldo_apos=2758.20, saldo_tipo=C
  → 2ª: valor 2.758,20 alinhado sob "Débito"   → valor_debito=2758.20, valor_credito=0, saldo_apos=0.00

  ATENÇÃO: nesse formato a coluna PREVALECE sobre as palavras-chave das regras 3 e 4.
  "VALOR A RECUPERAR" alinhado na coluna Débito é DÉBITO, mesmo contendo "VALOR".
  Um lançamento nunca tem valor_debito e valor_credito ambos zerados: se você leu um
  número na linha, ele pertence a Débito ou a Crédito conforme a coluna.

REGRAS:
1. Valores em formato brasileiro (1.234,56) → retorne como float padrão (1234.56)
2. Ignore linhas que são apenas o nome do fornecedor/empresa repetido (ruído do PDF)
3. DÉBITO (valor_debito > 0) = pagamento: SISPAG, BOLETO, TED, PIX, PGTO, VLR REF PGTO, PAGAMENTO, BAIXA, TRANSF, DOC
4. CRÉDITO (valor_credito > 0) = provisão/compra: NF, NOTA FISCAL, CT-E, COMPRA, CONFORME, SERVIÇO, AQUISIÇÃO, VALOR REFERENTE, FGTS A RECOLHER, SALDO DE IMPLANTAÇÃO, FÉRIAS, 13O SALÁRIO
5. "SALDO ANTERIOR" → saldo_anterior + saldo_anterior_tipo
6. "Total da conta: X Y" → X=total_debito, Y=total_credito. LEIA DIRETAMENTE — nunca calcule
7. Saldo com sufixo C=credor, D=devedor
8. Nos Formatos 2 e 5: o valor numérico da linha SEM data é o saldo resultante (saldo_apos), NÃO o valor do lançamento
9. Não invente valores; se incerto, use 0
10. tipo_operacao: COMPRA | PAGAMENTO | DEVOLUCAO | DEBITO | CREDITO
11. DEDUPLICAÇÃO: quando o mesmo texto aparece 3+ vezes na mesma linha, é histórico repetido — use-o uma única vez e limpo
12. HISTÓRICO FRAGMENTADO: quando uma linha sem data parece continuação de texto da linha anterior (sem números independentes), concatene-as com espaço para formar o histórico completo
13. COLUNA MANDA: se o bloco começa com o header da tabela (Formato 6), use a posição horizontal do número para decidir entre valor_debito e valor_credito — as palavras-chave das regras 3 e 4 são apenas desempate quando não há header
14. O total_debito informado em "Total da conta" deve fechar com a soma dos valor_debito dos lançamentos; se não fechar, reveja qual coluna cada número ocupa antes de responder

Retorne APENAS JSON válido, sem comentários:
{
  "saldo_anterior": 0.0,
  "saldo_anterior_tipo": "",
  "total_debito": 0.0,
  "total_credito": 0.0,
  "lancamentos": [
    {
      "data": "DD/MM/YYYY",
      "lote": "string",
      "historico": "texto limpo sem repetições",
      "conta_partida": "número ou null",
      "valor_debito": 0.0,
      "valor_credito": 0.0,
      "saldo_apos": 0.0,
      "saldo_tipo": "C ou D ou vazio",
      "tipo_operacao": "COMPRA"
    }
  ]
}"""

# ---------------------------------------------------------------------------
# Prompt de EXTRAÇÃO + CONCILIAÇÃO — usado pelo gpt-4o no Vision.
# ---------------------------------------------------------------------------
VISION_SYSTEM_PROMPT = """Você é um especialista em conciliação contábil de fornecedores, com foco em análise de razão contábil brasileiro.

Sua tarefa é:
1. Extrair os lançamentos do bloco de fornecedor visível na imagem
2. Conciliar compras com pagamentos (por NF direta ou FIFO cronológico)
3. Retornar o resultado estruturado em JSON

FORMATOS DE TABELA POSSÍVEIS:

Formato 1 — linha única (histórico + valores na mesma linha):
  "10/01/2025 4 COMPRAS CONFORME NF. Nº 21100 55 4.524,08 8.654,11C"
  → data=10/01/2025, lote=4, historico="COMPRAS CONFORME NF. Nº 21100", conta_partida=55, valor_credito=4524.08, saldo_apos=8654.11, saldo_tipo=C

Formato 2 — histórico ANTES da linha de data:
  "COMPRA CONFORME NF NÚMERO 1263290 DE 13.101,10C"   ← histórico + saldo (SEM data)
  "26/04/2024 7459 902 13.101,10"                     ← data + lote + CPC + valor real
  → data=26/04/2024, lote=7459, historico="COMPRA CONFORME NF NÚMERO 1263290 DE", conta_partida=902, valor_credito=13101.10

Formato 3 — pagamento com histórico repetido intercalado:
  "09/05/2024 SISPAG BOLETO BANCO 341 7715 SISPAG BOLETO BANCO 341 552 4.150,00 SISPAG BOLETO BANCO 341 35.166,01C"
  → data=09/05/2024, lote=7715, historico="SISPAG BOLETO BANCO 341", conta_partida=552, valor_debito=4150.00

Formato 4 — histórico repetido N vezes na mesma linha (layout multi-coluna do PDF):
  "20/05/2024 VLR REF PGTO FGTS 04/2024 1167 VLR REF PGTO FGTS 04/2024 VLR REF PGTO FGTS 04/2024 9 1.118,34 VLR REF PGTO FGTS 04/2024 4.095,27C"
  → data=20/05/2024, lote=1167, historico="VLR REF PGTO FGTS 04/2024" (uma só vez), conta_partida=9, valor_debito=1118.34, saldo_apos=4095.27

Formato 5 — histórico fragmentado em 2 linhas antes da data (coluna estreita):
  "VALOR REFERENTE FGTS S/FOLHA DE 5.154,41C"   ← início do histórico + saldo (ignore o número)
  "PAGAMENTO - 05/2024"                          ← fim do histórico
  "31/05/2024 370 1.059,14"                      ← data + lote + valor REAL
  → data=31/05/2024, lote=370, historico="VALOR REFERENTE FGTS S/FOLHA DE PAGAMENTO - 05/2024", valor_credito=1059.14, saldo_apos=5154.41

REGRAS DE EXTRAÇÃO:
1. Valores em formato brasileiro (1.234,56) → float padrão (1234.56)
2. Ignore texto repetido do nome do fornecedor/empresa (ruído do PDF)
3. DÉBITO = pagamento: SISPAG, BOLETO, TED, PIX, PGTO, VLR REF PGTO, PAGAMENTO, BAIXA, TRANSF, DOC
4. CRÉDITO = provisão/compra: NF, NOTA FISCAL, CT-E, COMPRA, CONFORME, SERVIÇO, AQUISIÇÃO, VALOR REFERENTE, FGTS A RECOLHER, SALDO DE IMPLANTAÇÃO, FÉRIAS, 13O SALÁRIO
5. "SALDO ANTERIOR" → saldo_anterior
6. "Total da conta: X Y" → leia diretamente, nunca calcule
7. Saldo com sufixo C=credor, D=devedor
8. Se a imagem mostrar múltiplos fornecedores, extraia APENAS o fornecedor indicado no contexto
9. tipo_operacao: COMPRA | PAGAMENTO | DEVOLUCAO | DEBITO | CREDITO
10. DEDUPLICAÇÃO: quando o mesmo texto aparece 3+ vezes na mesma linha, é histórico repetido — use-o uma única vez
11. HISTÓRICO FRAGMENTADO: linhas sem data que precedem a data real fazem parte do histórico — concatene-as

REGRAS DE CONCILIAÇÃO:
REGRA 1 — Casamento direto por NF: se pagamento mencionar NF → criterio="regra_1"
REGRA 2 — FIFO cronológico: aplique pagamento na compra mais antiga em aberto → criterio="regra_2"
  - pagamento < compra → parcialmente_paga
  - pagamento = compra → paga
  - pagamento > compra → quite a atual e aplique restante na próxima

Retorne APENAS JSON válido, sem comentários:
{
  "saldo_anterior": 0.0,
  "saldo_anterior_tipo": "C ou D ou vazio",
  "total_debito": 0.0,
  "total_credito": 0.0,
  "lancamentos": [
    {
      "data": "DD/MM/YYYY",
      "lote": "string",
      "historico": "texto limpo",
      "conta_partida": "número ou null",
      "valor_debito": 0.0,
      "valor_credito": 0.0,
      "saldo_apos": 0.0,
      "saldo_tipo": "C ou D ou vazio",
      "tipo_operacao": "COMPRA"
    }
  ],
  "conciliacao": [
    {
      "nf": "string ou null",
      "data_compra": "DD/MM/YYYY",
      "valor_original": 0.0,
      "valor_pago": 0.0,
      "saldo_em_aberto": 0.0,
      "status": "paga | parcialmente_paga | em_aberto",
      "pagamentos": [{"data": "DD/MM/YYYY", "valor": 0.0, "criterio": "regra_1 | regra_2"}]
    }
  ],
  "resumo": {"total_compras": 0.0, "total_pagamentos": 0.0, "saldo_em_aberto": 0.0},
  "validacao": {"saldo_confere": true, "observacoes": []}
}"""

# ---------------------------------------------------------------------------
# Prompt secundário — classificação de lançamentos incertos
# ---------------------------------------------------------------------------
CLASSIFY_SYSTEM_PROMPT = """Você é um assistente contábil especializado em lançamentos do Razão de Fornecedores brasileiro.

Classifique cada lançamento:
- COMPRA: aquisição de mercadoria ou serviço (crédito ao fornecedor)
- PAGAMENTO: quitação via boleto, transferência, SISPAG, TED, DOC, PIX
- DEVOLUCAO: devolução ou estorno
- DEBITO: débito genérico não identificável
- CREDITO: crédito genérico não identificável

Extraia também o número da NF/CT-e se houver (apenas dígitos).

Responda EXCLUSIVAMENTE com JSON:
{"resultados": [{"index": 0, "tipo_operacao": "PAGAMENTO", "numero_nf": null}]}"""


# ---------------------------------------------------------------------------
# Cliente OpenAI (singleton lazy)
# ---------------------------------------------------------------------------
def _get_client():
    """Instancia o cliente OpenAI. Retorna None se a chave não estiver configurada."""
    try:
        from openai import OpenAI
        from src.core.config import get_settings

        settings = get_settings()
        # Usa o Settings (que lê o .env) em vez de os.getenv (que não carrega .env automaticamente)
        secret = settings.openai_api_key
        api_key = secret.get_secret_value() if secret else None

        if not api_key:
            logger.warning("OPENAI_API_KEY não configurada — IA desativada.")
            return None
        return OpenAI(api_key=api_key)
    except ImportError:
        logger.warning("Pacote 'openai' não instalado — IA desativada.")
        return None


# ---------------------------------------------------------------------------
# Parsing completo de bloco via IA
# ---------------------------------------------------------------------------
def parsear_bloco_fornecedor_ia(bloco_texto: str) -> Optional[dict]:
    """
    Envia o texto bruto de um bloco de fornecedor ao GPT-4o-mini e retorna
    o dict com saldo_anterior, total_debito, total_credito e lancamentos.
    Retorna None se a IA não estiver configurada ou falhar.
    """
    if not bloco_texto or len(bloco_texto.strip()) < 30:
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": bloco_texto},
            ],
            max_tokens=16000,
            temperature=0,
            timeout=90,  # 90s máximo por bloco
        )

        data = json.loads(response.choices[0].message.content)

        if not isinstance(data.get("lancamentos"), list):
            logger.warning("⚠️ IA retornou JSON sem lista 'lancamentos'.")
            return None

        usage = response.usage
        n_lanc = len(data.get("lancamentos", []))
        print(
            f"🤖 IA respondeu: {n_lanc} lançamentos | "
            f"débito={data.get('total_debito')} | crédito={data.get('total_credito')} | "
            f"tokens: {usage.prompt_tokens if usage else 0} in/"
            f"{usage.completion_tokens if usage else 0} out"
        )
        if n_lanc > 0:
            print(f"   Primeiro lançamento bruto: {data['lancamentos'][0]}")
        return data

    except Exception as exc:
        logger.warning("⚠️ parsear_bloco_fornecedor_ia falhou: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Parsing via Vision — helper de um único batch (≤ 3 páginas)
# ---------------------------------------------------------------------------
def _visao_batch(client, png_bytes_list: list, bloco_texto: str = "") -> dict:
    """Envia até 3 imagens PNG ao gpt-4o Vision e retorna o dict parseado."""
    import base64

    content: list = []

    if bloco_texto and len(bloco_texto.strip()) > 20:
        content.append({
            "type": "text",
            "text": (
                "Texto extraído (pode estar corrompido — use as imagens como fonte primária):\n"
                + bloco_texto[:800]
                + "\n\nAnalise as imagens e extraia os lançamentos:"
            ),
        })

    for png in png_bytes_list[:3]:
        b64 = base64.b64encode(png).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user",   "content": content},
            ],
            max_tokens=16000,
            temperature=0,
            timeout=120,  # 120s máximo para Vision
        )

        data = json.loads(response.choices[0].message.content)
        if not isinstance(data.get("lancamentos"), list):
            raise VisionBatchError("Vision retornou um batch incompleto")

        usage = response.usage
        n_lanc = len(data.get("lancamentos", []))
        print(
            f"👁️ Vision IA: {n_lanc} lançamentos | "
            f"débito={data.get('total_debito')} | crédito={data.get('total_credito')} | "
            f"tokens: {usage.prompt_tokens if usage else 0}in/"
            f"{usage.completion_tokens if usage else 0}out"
        )
        if n_lanc > 0:
            print(f"   Primeiro (vision): {data['lancamentos'][0]}")
        return data

    except VisionBatchError:
        logger.exception("Vision retornou batch inválido")
        raise
    except Exception as exc:
        logger.exception("Falha ao processar batch Vision")
        raise VisionBatchError("Falha ao processar um batch Vision") from exc


# ---------------------------------------------------------------------------
# Parsing via Vision (primário para todos os blocos)
# ---------------------------------------------------------------------------
def parsear_bloco_fornecedor_ia_visao(png_bytes_list: list, bloco_texto: str = "") -> Optional[dict]:
    """
    Envia imagens de páginas ao gpt-4o Vision.
    Para blocos com muitas páginas, pagina automaticamente em batches de 3
    e acumula os lançamentos de todos os batches.
    """
    if not png_bytes_list:
        return None

    client = _get_client()
    if client is None:
        return None

    BATCH_SIZE = 3

    if len(png_bytes_list) <= BATCH_SIZE:
        return _visao_batch(client, png_bytes_list, bloco_texto)

    n_batches = (len(png_bytes_list) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"   📦 {len(png_bytes_list)} páginas → {n_batches} batches Vision (gpt-4o)...")

    todos_lancamentos: list = []
    saldo_anterior = 0.0
    saldo_anterior_tipo = ""
    total_debito = 0.0
    total_credito = 0.0

    for batch_idx in range(n_batches):
        start = batch_idx * BATCH_SIZE
        batch = png_bytes_list[start : start + BATCH_SIZE]
        print(f"   📦 Batch {batch_idx + 1}/{n_batches} (págs {start}–{start + len(batch) - 1})...")

        ctx = bloco_texto if batch_idx == 0 else ""
        try:
            resultado = _visao_batch(client, batch, ctx)
        except VisionBatchError as exc:
            raise VisionBatchError(
                f"Falha no batch Vision {batch_idx + 1} de {n_batches}"
            ) from exc

        if batch_idx == 0:
            saldo_anterior = resultado.get("saldo_anterior") or 0.0
            saldo_anterior_tipo = resultado.get("saldo_anterior_tipo") or ""
        todos_lancamentos.extend(resultado["lancamentos"])
        if resultado.get("total_debito"):
            total_debito = resultado["total_debito"]
        if resultado.get("total_credito"):
            total_credito = resultado["total_credito"]

    if not todos_lancamentos:
        return None

    return {
        "saldo_anterior": saldo_anterior,
        "saldo_anterior_tipo": saldo_anterior_tipo,
        "total_debito": total_debito,
        "total_credito": total_credito,
        "lancamentos": todos_lancamentos,
    }


# ---------------------------------------------------------------------------
# Classificação secundária de lançamentos incertos
# ---------------------------------------------------------------------------
def classificar_lancamentos_incertos(lancamentos: list) -> None:
    """
    Reclassifica em batch os lançamentos marcados como classificacao_incerta=True.
    Atualiza os dicts in-place. Falha silenciosa.
    """
    incertos = [l for l in lancamentos if l.get("classificacao_incerta")]
    if not incertos:
        return

    client = _get_client()
    if client is None:
        return

    itens = []
    for i, lanc in enumerate(incertos):
        debito = float(lanc.get("valor_debito", 0))
        credito = float(lanc.get("valor_credito", 0))
        historico = (lanc.get("historico") or "").strip()
        itens.append(
            f'{i}. Histórico: "{historico}" | Débito: {debito:.2f} | Crédito: {credito:.2f}'
        )

    user_message = "Classifique os lançamentos:\n\n" + "\n".join(itens)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=1024,
            temperature=0,
        )

        data = json.loads(response.choices[0].message.content)
        for item in data.get("resultados", []):
            idx = item.get("index")
            if idx is None or idx >= len(incertos):
                continue

            lanc = incertos[idx]
            tipo_ia = item.get("tipo_operacao", "").upper()
            nf_ia: Optional[str] = item.get("numero_nf")

            if tipo_ia in ("COMPRA", "PAGAMENTO", "DEVOLUCAO", "DEBITO", "CREDITO", "OUTRO"):
                lanc["tipo_operacao"] = tipo_ia
                lanc["classificado_por_ia"] = True

            if nf_ia and not lanc.get("numero_nf"):
                nf_str = str(nf_ia).strip()
                if len(nf_str) >= 4:
                    lanc["numero_nf"] = nf_str
                    lanc["classificado_por_ia"] = True

        logger.info(
            "✅ Classificação secundária: %d/%d lançamentos atualizados.",
            sum(1 for l in incertos if l.get("classificado_por_ia")),
            len(incertos),
        )

    except Exception as exc:
        logger.warning("⚠️ classificar_lancamentos_incertos falhou: %s", exc)
