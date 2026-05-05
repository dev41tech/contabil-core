"""
Diagnóstico do OCR: mostra exatamente o que o GPT-4o retorna para o PDF do Itaú.
Execução: cd contabil-core && .venv/Scripts/python scripts/test_ocr.py
"""
import sys
import base64
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

PDF_PATH = Path("C:/Users/nathan.carvalho/Downloads/Extrato_Itaú_01_2026.pdf")

# ── carrega config
from src.core.config import get_settings
s = get_settings()
if not s.openai_enabled:
    print("ERRO: OPENAI_API_KEY não configurada no .env")
    sys.exit(1)

# ── renderiza página 0 com PyMuPDF
import fitz
print(f"Abrindo PDF: {PDF_PATH}")
doc = fitz.open(str(PDF_PATH))
print(f"Páginas: {doc.page_count}")
page = doc.load_page(0)
mat = fitz.Matrix(150 / 72, 150 / 72)
pix = page.get_pixmap(matrix=mat, alpha=False)
png_bytes = pix.tobytes("png")
doc.close()
print(f"PNG página 0: {len(png_bytes):,} bytes ({pix.width}x{pix.height}px)")

# ── salva PNG para inspeção visual
out_png = Path("scripts/debug_page0.png")
out_png.write_bytes(png_bytes)
print(f"PNG salvo em: {out_png} - abra para ver o que o GPT-4o esta recebendo")

# ── chama GPT-4o
from openai import OpenAI
client = OpenAI(api_key=s.openai_api_key.get_secret_value())

img_b64 = base64.standard_b64encode(png_bytes).decode()

_SYSTEM = "Você é um extrator de dados financeiros. Responda SOMENTE com JSON puro — sem texto, sem markdown, sem explicações."

_PROMPT = """\
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

print("\n== Enviando para GPT-4o (aguarde)...")
response = client.chat.completions.create(
    model="gpt-4o",
    max_tokens=4096,
    temperature=0,
    messages=[
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"},
                },
                {"type": "text", "text": _PROMPT},
            ],
        },
    ],
)

raw = response.choices[0].message.content.strip()
print(f"\n== Resposta BRUTA do GPT-4o ({len(raw)} chars):\n")
print(raw[:3000])

# ── tenta parsear
import re
cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
try:
    data = json.loads(cleaned)
    if isinstance(data, list):
        print(f"\n== JSON parseado OK: {len(data)} itens")
        for i, item in enumerate(data[:5]):
            print(f"  [{i}] {item}")
        if len(data) > 5:
            print(f"  ... +{len(data)-5} itens")
    else:
        print(f"\n== JSON e {type(data).__name__} (nao lista): {data}")
except json.JSONDecodeError as e:
    print(f"\n== FALHA ao parsear JSON: {e}")
    print("Conteúdo limpo:", cleaned[:500])
