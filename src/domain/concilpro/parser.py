"""
Parser para extrair dados do Razão de Fornecedores (PDF).
Estratégia: IA (GPT-4o) Vision como primário; GPT-4o-mini texto como fallback.
"""
import hashlib
import logging
import re
import zipfile
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import pdfplumber
from collections import Counter

logger = logging.getLogger(__name__)


def _deduplicar_historico_linha(linha: str) -> str:
    """
    Remove repetições de substrings causadas pela extração de PDFs com layout multi-coluna.

    Alguns sistemas geram PDFs onde o histórico aparece em múltiplas colunas que
    colapsam numa só linha no pdfplumber:
      "20/05/2024 VLR REF PGTO FGTS 04/2024 1167 VLR REF PGTO FGTS 04/2024 VLR REF PGTO FGTS 04/2024 9 1.118,34 VLR REF PGTO FGTS 04/2024 4.095,27C"
      → "20/05/2024 VLR REF PGTO FGTS 04/2024 1167                                9 1.118,34                                4.095,27C"

    As repetições são substituídas por espaços, não removidas: a coluna em que
    um valor cai é o que distingue Débito de Crédito.
    """
    palavras = linha.split()
    if len(palavras) < 9:
        return linha

    # Tenta n-gramas de 3 a 7 palavras; escolhe o mais frequente com ≥3 ocorrências
    melhor_frase: str = ""
    melhor_contagem: int = 0

    for n in range(7, 2, -1):
        if n >= len(palavras):
            continue
        ngramas = [" ".join(palavras[i : i + n]) for i in range(len(palavras) - n + 1)]
        contagens = Counter(ngramas)
        frase, cnt = contagens.most_common(1)[0]
        if cnt >= 3 and cnt > melhor_contagem:
            melhor_frase = frase
            melhor_contagem = cnt

    if not melhor_frase:
        return linha

    # Mantém a PRIMEIRA ocorrência e remove as demais
    padrao = re.compile(r"(?<!\w)" + re.escape(melhor_frase) + r"(?!\w)")
    ocorrencias = [m.start() for m in padrao.finditer(linha)]
    if len(ocorrencias) < 3:
        return linha

    # Substitui da segunda ocorrência em diante por espaços do mesmo tamanho,
    # em vez de deletar: o alinhamento horizontal identifica a coluna
    # (Débito x Crédito) e precisa sobreviver à limpeza.
    resultado = linha
    for pos in reversed(ocorrencias[1:]):
        fim = pos + len(melhor_frase)
        resultado = resultado[:pos] + (" " * len(melhor_frase)) + resultado[fim:]

    return resultado.rstrip()


def _preprocessar_bloco(bloco_texto: str) -> str:
    """
    Limpa o texto bruto de um bloco antes de enviar para a IA:
    - Remove repetições de histórico por linha (Formato 4)
    - Não altera linhas curtas nem linhas sem texto significativo
    """
    linhas_limpas = []
    for linha in bloco_texto.split("\n"):
        linhas_limpas.append(_deduplicar_historico_linha(linha))
    return "\n".join(linhas_limpas)


def calcular_hash_arquivo(arquivo_bytes: bytes) -> str:
    """Calcula hash SHA256 do arquivo para evitar duplicação."""
    return hashlib.sha256(arquivo_bytes).hexdigest()


def parse_valor(texto: str) -> Decimal:
    """Converte string de valor para Decimal. Ex: "1.234,56" → 1234.56"""
    if not texto or texto.strip() == "":
        return Decimal("0")
    texto = texto.strip().replace(".", "").replace(",", ".")
    texto = re.sub(r'[^\d.-]', '', texto)
    try:
        return Decimal(texto)
    except Exception:
        return Decimal("0")


def parse_data(texto: str) -> Optional[datetime]:
    """Converte string de data para datetime. Ex: "31/01/2025" → datetime(2025, 1, 31)"""
    try:
        return datetime.strptime(texto.strip(), "%d/%m/%Y")
    except Exception:
        return None


def extrair_numero_nf(historico: str) -> Optional[str]:
    """Extrai número de NF/CT-e do histórico."""
    patterns = [
        r'NF\.?\s*N[oºº°]?\s*(\d+)',
        r'NF\s+(\d+)',
        r'REF\s+(?:REF\s+)?NF\s+(\d+)',
        r'REF\s+(?:REF\s+)?(\d+)',
        r'CT-E\s*(\d+)',
        r'NOTA\s*FISCAL\s*(\d+)',
        r'CONFORME\s+NF[.\s]*(\d+)',
        r'^(\d{5,6})\s*-',
        r'CONFORME\s+NF\s+N[ÚU]MERO\s+(\d+)',
        r'CONF\.\s*NFS\s*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, historico, re.IGNORECASE)
        if match:
            nf_num = match.group(1)
            if len(nf_num) >= 4:
                return nf_num
    return None


def extrair_cnpj(historico: str) -> Optional[str]:
    """Extrai CNPJ do histórico se presente."""
    pattern = r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})'
    match = re.search(pattern, historico)
    return match.group(1) if match else None


def classificar_tipo_operacao(historico: str, valor_debito: Decimal, valor_credito: Decimal) -> str:
    """Classifica o tipo de operação baseado no histórico e valores."""
    historico_upper = historico.upper()

    if valor_debito > 0:
        if any(palavra in historico_upper for palavra in [
            "PGTO", "PAGAMENTO", "BAIXA", "VLR REF", "VALOR REF"
        ]):
            return "PAGAMENTO"
        elif "DEVOLUCAO" in historico_upper or "ESTORNO" in historico_upper:
            return "DEVOLUCAO"
        else:
            return "DEBITO"

    elif valor_credito > 0:
        if any(palavra in historico_upper for palavra in [
            "COMPRA", "NF", "NOTA FISCAL", "SERVICO", "SERVIÇO",
            "CT-E", "ADQUIRIDO", "AQUISICAO", "AQUISIÇÃO", "CONFORME"
        ]):
            return "COMPRA"
        elif "ADTO" in historico_upper or "ADIANTAMENTO" in historico_upper:
            return "ADIANTAMENTO"
        else:
            return "CREDITO"

    return "OUTRO"


def _extrair_pagina_por_palavras(pagina) -> str:
    """
    Extrai texto de uma página agrupando palavras por posição y (linha).
    """
    try:
        words = pagina.extract_words(x_tolerance=3, y_tolerance=3)
        if not words:
            return ""

        bucket = 5
        linhas: dict = {}
        for word in words:
            y_key = int(word["top"] // bucket) * bucket
            if y_key not in linhas:
                linhas[y_key] = []
            linhas[y_key].append(word)

        resultado = []
        for y in sorted(linhas.keys()):
            palavras = sorted(linhas[y], key=lambda w: w["x0"])
            resultado.append("  ".join(w["text"] for w in palavras))

        return "\n".join(resultado)
    except Exception as exc:
        print(f"⚠️ Extração por palavras falhou: {exc}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Reconstrução por caracteres (Formato 6 — baselines sobrepostas)
#
# Alguns sistemas emitem o Razão com o histórico desenhado várias vezes na
# mesma linha, em baselines separadas por menos de 1 pt. Como isso cai dentro
# do y_tolerance do pdfplumber, `extract_text()` funde as duas baselines e,
# ordenando por x, ENTRELAÇA os caracteres:
#
#   top=112.177 → "VALOR A RECUPERAR DE IPI DO PERÍODO" (5 cópias em x)
#   top=113.137 → "28/02/2026" "11192" "29" "2.758,20" "0,00"
#   resultado   → "2V8A/L0O2R/2 A02 R6ECUPERA1R1 1D9E2 IVPIA LDOOR..."
#
# A IA descarta a linha e `_recuperar_lancamentos_ocultos` fabrica uma entrada
# genérica no lugar — perdendo lote, contrapartida e histórico reais.
#
# Cada campo, porém, é contíguo no content stream. Reagrupando os chars em
# "runs" (sequências contíguas na mesma baseline) o entrelaçamento desaparece.
# ─────────────────────────────────────────────────────────────────────────────

_RUN_GAP_MAX = 2.0      # gap horizontal máximo (pt) entre chars de um mesmo run
_RUN_BASELINE = 0.3     # variação de 'top' tolerada dentro de um run
_LINHA_TOL = 2.5        # variação de 'top' para runs da mesma linha visual


def _construir_runs(chars: List[Dict]) -> List[Dict]:
    """
    Quebra os chars da página em runs: sequências contíguas no content stream,
    na mesma baseline e com x avançando. Cada run é um campo atômico
    ('28/02/2026', '11192', 'VALOR A RECUPERAR DE IPI DO PERÍODO').
    """
    runs: List[Dict] = []
    atual: List[Dict] = []

    for char in chars:
        if not atual:
            atual = [char]
            continue

        anterior = atual[-1]
        mesma_baseline = abs(char["top"] - anterior["top"]) < _RUN_BASELINE
        avanca_x = char["x0"] >= anterior["x0"] - 0.5
        gap_ok = (char["x0"] - anterior["x1"]) < _RUN_GAP_MAX

        if mesma_baseline and avanca_x and gap_ok:
            atual.append(char)
        else:
            runs.append(atual)
            atual = [char]

    if atual:
        runs.append(atual)

    return [{
        "texto": "".join(c["text"] for c in run),
        "x0": min(c["x0"] for c in run),
        "x1": max(c["x1"] for c in run),
        "top": min(c["top"] for c in run),
    } for run in runs]


_RE_SO_NUMERO = re.compile(r"[\d.,]+[CD]?")


def _runs_sobrepostos(a: Dict, b: Dict) -> bool:
    """Diz se dois runs ocupam faixas horizontais que se cruzam."""
    return a["x0"] < b["x1"] and b["x0"] < a["x1"]


def _montar_linhas_de_runs(runs: List[Dict], largura_char: float = 3.7) -> List[str]:
    """
    Agrupa runs em linhas visuais e remove cópias duplicadas de renderização.

    Cópias de texto idêntico que SE SOBREPÕEM em x são artefato — mantém-se a
    que menos colide com os campos reais. Cópias que NÃO se sobrepõem são
    colunas legítimas com o mesmo valor (ex: "Total da conta: 40.679,43
    40.679,43") e são todas preservadas.

    O alinhamento horizontal é PRESERVADO por padding: é a posição x que
    distingue a coluna Débito da Crédito. No Razão do exemplo o header tem
    'Débito' em x=389 e 'Crédito' em x=447; a linha de crédito traz o valor em
    x=449 e a de débito em x=388. Colapsando tudo em espaço simples, a IA
    perde o sinal e classifica todo lançamento como crédito.
    """
    linhas: List[List[Dict]] = []
    for run in sorted(runs, key=lambda r: r["top"]):
        if linhas and abs(run["top"] - linhas[-1][0]["top"]) < _LINHA_TOL:
            linhas[-1].append(run)
        else:
            linhas.append([run])

    resultado: List[str] = []
    for grupo in linhas:
        por_texto: Dict[str, List[Dict]] = defaultdict(list)
        for run in grupo:
            por_texto[run["texto"]].append(run)

        mantidos: List[Dict] = []
        for texto, copias in por_texto.items():
            if len(copias) == 1:
                mantidos.append(copias[0])
                continue

            unicos = [r for r in grupo if len(por_texto[r["texto"]]) == 1]

            def _colisoes(run: Dict) -> int:
                return sum(1 for o in unicos if _runs_sobrepostos(run, o))

            if len(copias) >= 3:
                # 3+ cópias do mesmo texto na mesma linha é artefato de
                # renderização. Mantém só a que não colide com campo real —
                # as demais empurrariam os valores para fora de suas colunas.
                #
                # Trade-off aceito: uma linha legítima com 3 colunas de valor
                # idêntico perderia duas. Na prática o saldo carrega sufixo
                # C/D (texto diferente) e a guarda de divergência no
                # persistidor pega qualquer total que não feche.
                mantidos.append(min(copias, key=lambda c: (_colisoes(c), c["x0"])))
                continue

            # Exatamente 2 cópias: se não se sobrepõem, normalmente são colunas
            # legítimas com o mesmo valor ("Total da conta: 40.679,43 40.679,43").
            a, b = sorted(copias, key=lambda r: r["x0"])
            if _runs_sobrepostos(a, b):
                mantidos.append(min((a, b), key=lambda c: (_colisoes(c), c["x0"])))
                continue

            # Exceção: as colunas de valor contêm números. Uma cópia de TEXTO
            # pousada sobre um número é artefato de renderização — e mantê-la
            # empurra o valor para fora da sua coluna, que é justamente o sinal
            # que distingue Débito de Crédito.
            numericos = [r for r in grupo if _RE_SO_NUMERO.fullmatch(r["texto"])]
            if not _RE_SO_NUMERO.fullmatch(texto):
                sobre_numero = [c for c in (a, b)
                                if any(_runs_sobrepostos(c, n) for n in numericos)]
                if sobre_numero and len(sobre_numero) < 2:
                    mantidos.append(a if sobre_numero[0] is b else b)
                    continue

            mantidos.extend((a, b))

        mantidos.sort(key=lambda r: r["x0"])

        linha = ""
        for run in mantidos:
            coluna = int(round(run["x0"] / largura_char))
            if linha and len(linha) >= coluna:
                # a coluna alvo já foi ocupada: separa com um espaço em vez de
                # colar os textos (colar cria tokens falsos tipo "2.758,20VALOR")
                linha += " "
            else:
                linha = linha.ljust(coluna)
            linha += run["texto"]
        resultado.append(linha.rstrip())

    return resultado


def _extrair_pagina_por_chars(pagina) -> str:
    """Extrai texto reconstruindo as linhas a partir dos chars crus."""
    try:
        chars = pagina.chars
        if not chars:
            return ""

        # largura mediana de char da própria página — base para o grid de colunas
        larguras = sorted(c["x1"] - c["x0"] for c in chars if c["text"].strip())
        largura_char = larguras[len(larguras) // 2] if larguras else 3.7

        return "\n".join(_montar_linhas_de_runs(_construir_runs(chars), largura_char))
    except Exception as exc:
        print(f"⚠️ Extração por chars falhou: {exc}")
        return ""


_RE_LINHA_LANCAMENTO = re.compile(r"^\s*\d{2}/\d{2}/\d{4}\b")
_RE_NUMERICO_PURO = re.compile(r"[\d./,\-]+[CD]?$")
_RE_TEM_DIGITO = re.compile(r"\d")
_RE_TEM_MAIUSCULA = re.compile(r"[A-ZÁÉÍÓÚÂÊÔÃÕÇ]")


def _pontuar_extracao(texto: str) -> Tuple[int, int]:
    """
    Mede a qualidade de uma extração para fins de escolha entre estratégias.

    Retorna (linhas de lançamento, tokens corrompidos). Comprimento NÃO serve
    como critério: o entrelaçamento duplica o texto e infla o tamanho, então a
    extração pior sempre venceria.
    """
    linhas = texto.split("\n")
    lancamentos = sum(1 for l in linhas if _RE_LINHA_LANCAMENTO.match(l))

    corrompidos = 0
    for linha in linhas:
        for token in linha.split():
            if len(token) < 6:
                continue
            # token que mistura dígito e maiúscula sem ser data/valor/conta
            if (_RE_TEM_DIGITO.search(token)
                    and _RE_TEM_MAIUSCULA.search(token)
                    and not _RE_NUMERICO_PURO.match(token)):
                corrompidos += 1

    return lancamentos, corrompidos


_ESTRATEGIAS = ("layout", "palavras", "chars")


def _extrair_candidatos(pagina) -> Dict[str, str]:
    """Roda as três estratégias de extração sobre a página."""
    return {
        "layout": pagina.extract_text(layout=True) or "",
        "palavras": _extrair_pagina_por_palavras(pagina),
        "chars": _extrair_pagina_por_chars(pagina),
    }


def _escolher_estrategia(paginas_candidatas: List[Dict[str, str]]) -> str:
    """
    Escolhe UMA estratégia para o documento inteiro, somando a qualidade de
    todas as páginas.

    A escolha não pode ser por página: cada estratégia produz uma grade
    horizontal própria, e o header da tabela — que define onde ficam as colunas
    Débito e Crédito — é localizado uma vez para o documento. Misturar
    estratégias faz as colunas do header não valerem para as páginas extraídas
    de outro jeito, e os valores caem fora de qualquer coluna.

    Comprimento não serve de critério: o entrelaçamento duplica texto e infla o
    tamanho, então a extração pior venceria.
    """
    melhor_nome, melhor_lanc, melhor_corrompidos = "", -1, 0

    for nome in _ESTRATEGIAS:
        textos = [p.get(nome) or "" for p in paginas_candidatas]
        if not any(textos):
            continue
        lancamentos = corrompidos = 0
        for texto in textos:
            l, c = _pontuar_extracao(texto)
            lancamentos += l
            corrompidos += c
        # só troca com ganho estrito: mais lançamentos, ou empate com menos ruído
        if (lancamentos > melhor_lanc
                or (lancamentos == melhor_lanc and corrompidos < melhor_corrompidos)):
            melhor_nome, melhor_lanc, melhor_corrompidos = nome, lancamentos, corrompidos

    if melhor_nome:
        print(
            f"   📏 Estratégia de extração: {melhor_nome} "
            f"({melhor_lanc} lançamentos, {melhor_corrompidos} tokens corrompidos)"
        )
    return melhor_nome or "layout"


def _extrair_paginas(pdf) -> List[str]:
    """Extrai todas as páginas do PDF com uma estratégia única e coerente."""
    candidatas = [_extrair_candidatos(pagina) for pagina in pdf.pages]
    estrategia = _escolher_estrategia(candidatas)
    return [
        c.get(estrategia) or c.get("layout") or ""
        for c in candidatas
    ]


def extrair_texto_pdf(arquivo_bytes: bytes) -> str:
    """
    Extrai texto de um PDF usando pdfplumber.
    """
    texto_completo = []

    try:
        with pdfplumber.open(BytesIO(arquivo_bytes)) as pdf:
            total_paginas = len(pdf.pages)
            print(f"📄 PDF detectado: {total_paginas} páginas")

            for texto in _extrair_paginas(pdf):
                if texto:
                    texto_completo.append(texto)

                if i % 10 == 0:
                    print(f"   Processadas {i}/{total_paginas} páginas...")

            print(f"✅ Extração concluída: {len(texto_completo)} páginas com texto")

    except Exception as e:
        raise ValueError(f"Erro ao extrair texto do PDF: {str(e)}")

    return "\n\n".join(texto_completo)


def detectar_formato_arquivo(arquivo_bytes: bytes) -> str:
    """Detecta se o arquivo é PDF, XLSX ou ZIP."""
    from src.domain.concilpro.planilha import e_planilha

    if arquivo_bytes[:4] == b'%PDF':
        return 'PDF'
    # XLSX também começa com 'PK' — é um ZIP. Testar a planilha antes.
    if e_planilha(arquivo_bytes):
        return 'XLSX'
    if arquivo_bytes[:2] == b'PK' or zipfile.is_zipfile(BytesIO(arquivo_bytes)):
        return 'ZIP'
    return 'PDF'


def _deduplicar_lancamentos(lancamentos: List[Dict]) -> Tuple[List[Dict], int]:
    """
    Remove o mesmo lançamento lido duas vezes por caminhos diferentes.

    Um fornecedor que atravessa a quebra de página vira dois blocos: o parcial
    costuma cair no fallback Vision (que lê a página inteira como imagem) e a
    continuação é lida pelo texto. Os dois caminhos discordam nos metadados —
    um traz a NF, o outro o lote, às vezes a data sai diferente — mas o valor e
    o saldo resultante coincidem.

    Duas assinaturas de duplicata:
      • mesma data + mesmos valores + mesmo saldo
      • mesmo lote + mesmos valores + mesmo saldo (pega o caso de data divergente)

    Saldo idêntico após dois lançamentos de valor não-zero é aritmeticamente
    impossível, então a coincidência denuncia a releitura.
    """
    resultado: List[Dict] = []
    por_data: Dict[tuple, Dict] = {}
    por_lote: Dict[tuple, Dict] = {}
    descartados = 0

    for lanc in lancamentos:
        deb = str(Decimal(str(lanc.get('valor_debito') or 0)))
        cred = str(Decimal(str(lanc.get('valor_credito') or 0)))
        saldo = str(Decimal(str(lanc.get('saldo_apos_lancamento') or 0)))
        lote = str(lanc.get('lote') or '').strip()

        chave_data = (str(lanc.get('data_lancamento')), deb, cred, saldo)
        chave_lote = (lote, deb, cred, saldo) if lote else None

        existente = por_data.get(chave_data)
        if existente is None and chave_lote:
            existente = por_lote.get(chave_lote)

        if existente is not None and (deb != "0" or cred != "0"):
            descartados += 1
            # preserva os metadados que só um dos caminhos capturou
            if not existente.get('numero_nf') and lanc.get('numero_nf'):
                existente['numero_nf'] = lanc['numero_nf']
            if not existente.get('lote') and lote:
                existente['lote'] = lanc['lote']
            continue

        resultado.append(lanc)
        por_data[chave_data] = lanc
        if chave_lote:
            por_lote[chave_lote] = lanc

    return resultado, descartados


def consolidar_fornecedores_duplicados(fornecedores: List[Dict]) -> List[Dict]:
    """
    Consolida fornecedores com mesmo código de conta quebrados entre páginas.
    """
    por_codigo: dict = defaultdict(list)
    for forn in fornecedores:
        codigo = forn.get('codigo_conta')
        if codigo:
            por_codigo[codigo].append(forn)

    fornecedores_consolidados = []

    for codigo, lista_forn in por_codigo.items():
        if len(lista_forn) == 1:
            # Mesmo sem blocos irmãos, o bloco pode ter sido lido por texto e
            # por Vision — a duplicata nasce dentro do próprio fornecedor.
            unico = lista_forn[0]
            limpos, descartados = _deduplicar_lancamentos(unico.get('lancamentos', []))
            if descartados:
                unico = unico.copy()
                unico['lancamentos'] = limpos
                print(f"      🔁 {descartados} lançamento(s) duplicado(s) descartado(s) "
                      f"em {str(unico.get('nome_fornecedor'))[:34]}")
            fornecedores_consolidados.append(unico)
        else:
            print(f"   🔧 Consolidando {len(lista_forn)} registros do código {codigo} - {lista_forn[0]['nome_fornecedor']}")

            consolidado = lista_forn[0].copy()

            brutos: List[Dict] = []
            for forn in lista_forn:
                brutos.extend(forn.get('lancamentos', []))

            todos_lancamentos, duplicados = _deduplicar_lancamentos(brutos)
            if duplicados:
                print(f"      🔁 {duplicados} lançamento(s) duplicado(s) entre páginas descartado(s)")

            consolidado['lancamentos'] = todos_lancamentos

            ultimo = lista_forn[-1]
            consolidado['total_debito'] = ultimo.get('total_debito', Decimal("0"))
            consolidado['total_credito'] = ultimo.get('total_credito', Decimal("0"))

            if 'saldo_anterior' in lista_forn[0]:
                consolidado['saldo_anterior'] = lista_forn[0]['saldo_anterior']
                consolidado['saldo_anterior_tipo'] = lista_forn[0].get('saldo_anterior_tipo', '')

            fornecedores_consolidados.append(consolidado)

    return fornecedores_consolidados


def _ia_decimal(val) -> Decimal:
    """Converte valor retornado pela IA para Decimal."""
    if val is None:
        return Decimal("0")
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = str(val).strip().replace(" ", "")
    try:
        return Decimal(s)
    except Exception:
        s = s.replace(".", "").replace(",", ".")
        try:
            return Decimal(s)
        except Exception:
            return Decimal("0")


def _ia_data(val) -> Optional[datetime]:
    """Converte data retornada pela IA para datetime."""
    if not val:
        return None
    s = str(val).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalizar_lancamento_ia(lanc_ia: dict) -> Optional[Dict]:
    """Converte um lançamento retornado pelo GPT para o formato interno."""
    try:
        data = _ia_data(lanc_ia.get("data"))
        if not data:
            return None

        vd = _ia_decimal(lanc_ia.get("valor_debito"))
        vc = _ia_decimal(lanc_ia.get("valor_credito"))
        saldo = _ia_decimal(lanc_ia.get("saldo_apos"))
        historico = (lanc_ia.get("historico") or "").strip()
        cpc = lanc_ia.get("conta_partida")
        cpc = str(cpc).strip() if cpc else None

        tipo_ia = (lanc_ia.get("tipo_operacao") or "").upper()
        if tipo_ia in ("COMPRA", "PAGAMENTO", "DEVOLUCAO", "DEBITO", "CREDITO", "OUTRO"):
            tipo = tipo_ia
        else:
            tipo = classificar_tipo_operacao(historico, vd, vc)

        nf = extrair_numero_nf(historico)
        cnpj = extrair_cnpj(historico)

        return {
            "data_lancamento": data,
            "lote": str(lanc_ia.get("lote", "") or ""),
            "historico": historico,
            "conta_partida": cpc,
            "valor_debito": vd,
            "valor_credito": vc,
            "saldo_apos_lancamento": saldo,
            "saldo_tipo": str(lanc_ia.get("saldo_tipo") or ""),
            "tipo_operacao": tipo,
            "numero_nf": nf,
            "cnpj_historico": cnpj,
            "classificacao_incerta": tipo in ("DEBITO", "CREDITO", "OUTRO"),
            "classificado_por_ia": True,
        }
    except Exception as exc:
        logger.warning("⚠️ _normalizar_lancamento_ia falhou: %s", exc)
        return None


_RE_VALOR_BR = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}(?=[CD]?(?:\s|$))")
_RE_ROTULO_COLUNA = re.compile(r"Débito|Crédito|Saldo-Exercício|Saldo")


def _colunas_do_header(linhas_bloco: List[str]) -> Optional[Dict]:
    """
    Mapeia as colunas do header da tabela.

    As colunas de valor são identificadas pela BORDA DIREITA do rótulo, porque
    números contábeis são alinhados à direita. A tolerância é derivada do
    espaçamento real entre colunas, não fixa: há layouts com 'Débito' e
    'Crédito' a 7 caracteres de distância, onde uma folga fixa de 8 faria um
    débito ser lido como crédito.
    """
    for linha in linhas_bloco:
        if "Débito" not in linha or "Crédito" not in linha:
            continue

        # todas as colunas monetárias, na ordem em que aparecem
        bordas: List[Tuple[str, int]] = []
        for achado in _RE_ROTULO_COLUNA.finditer(linha):
            rotulo = achado.group()
            nome = {"Débito": "debito", "Crédito": "credito"}.get(rotulo, "saldo")
            if nome == "saldo":
                nome = f"saldo{len([b for b in bordas if b[0].startswith('saldo')]) or ''}"
            bordas.append((nome, achado.end()))

        valores = {nome: fim for nome, fim in bordas}
        if "debito" not in valores or "credito" not in valores:
            continue

        posicoes = sorted(fim for _, fim in bordas)
        if len(posicoes) > 1:
            menor_gap = min(b - a for a, b in zip(posicoes, posicoes[1:]))
            tolerancia = max(2, min(8, menor_gap // 2))
        else:
            tolerancia = 8

        col_debito = linha.find("Débito")
        historico = linha.find("Histórico")
        cta = linha.find("Cta.C.Part.")

        return {
            "valores": valores,
            "tolerancia": tolerancia,
            "historico": historico if 0 <= historico < col_debito else None,
            "cta": cta if 0 <= cta < col_debito else None,
            "inicio_debito": col_debito,
        }
    return None


def _coluna_do_valor(fim_valor: int, colunas: Dict) -> Optional[str]:
    """Diz a que coluna um valor pertence, comparando bordas direitas."""
    alvo, menor = None, colunas["tolerancia"] + 1
    for nome, borda in colunas["valores"].items():
        distancia = abs(fim_valor - borda)
        if distancia < menor:
            alvo, menor = nome, distancia
    return alvo


def _valores_por_coluna(linha: str, colunas: Dict) -> Optional[Dict]:
    """
    Lê uma linha de lançamento e devolve os valores separados por coluna.

    Usa a posição horizontal — o alinhamento é preservado pela reconstrução, e
    é ele que distingue Débito de Crédito. Palavra-chave no histórico não serve:
    'VALOR A RECUPERAR' cai na coluna Débito num razão e na Crédito noutro.
    """
    m = re.match(r"^\s*(\d{2}/\d{2}/\d{4})\s+(\S+)", linha)
    if not m:
        return None

    debito = credito = Decimal("0")
    for achado in _RE_VALOR_BR.finditer(linha):
        # ignora a data/lote já consumidos no início da linha
        if achado.start() < m.end(2):
            continue
        try:
            valor = Decimal(achado.group().replace(".", "").replace(",", "."))
        except Exception:
            continue

        alvo = _coluna_do_valor(achado.end(), colunas)
        if alvo == "debito":
            debito += valor
        elif alvo == "credito":
            credito += valor

    if debito == 0 and credito == 0:
        return None
    return {"data": m.group(1), "lote": m.group(2), "debito": debito, "credito": credito}


def _corrigir_colunas_por_posicao(lancamentos: List[Dict], linhas_bloco: List[str]) -> List[Dict]:
    """
    Reatribui valor_debito/valor_credito pela coluna em que o número aparece.

    A IA decide isso por heurística de palavra-chave e é instável: o mesmo PDF
    processado duas vezes produziu 'a pagar' de R$ 2.758,20 e R$ 33.416,50.
    Quando o header de colunas existe, a posição resolve sem ambiguidade —
    então ela prevalece. Sem header (Formatos 1–5), mantém-se o que a IA disse.
    """
    colunas = _colunas_do_header(linhas_bloco)
    if not colunas:
        return lancamentos

    disponiveis: List[Dict] = []
    for linha in linhas_bloco:
        registro = _valores_por_coluna(linha, colunas)
        if registro:
            disponiveis.append(registro)

    if not disponiveis:
        return lancamentos

    usados: set = set()
    corrigidos = 0

    for lanc in lancamentos:
        data = lanc.get("data_lancamento")
        data_str = data.strftime("%d/%m/%Y") if hasattr(data, "strftime") else str(data or "")
        lote_str = str(lanc.get("lote") or "").strip()

        candidatos = [
            (i, r) for i, r in enumerate(disponiveis)
            if i not in usados and r["data"] == data_str and r["lote"] == lote_str
        ]
        if not candidatos:
            continue

        if len(candidatos) > 1:
            # mesma data e lote: desempata pelo valor que a IA já tinha lido
            atual = max(
                Decimal(str(lanc.get("valor_debito") or 0)),
                Decimal(str(lanc.get("valor_credito") or 0)),
            )
            candidatos.sort(key=lambda par: min(
                abs(par[1]["debito"] - atual), abs(par[1]["credito"] - atual)
            ))

        indice, registro = candidatos[0]
        usados.add(indice)

        if (Decimal(str(lanc.get("valor_debito") or 0)) != registro["debito"]
                or Decimal(str(lanc.get("valor_credito") or 0)) != registro["credito"]):
            lanc["valor_debito"] = registro["debito"]
            lanc["valor_credito"] = registro["credito"]
            if registro["debito"] > 0 and lanc.get("tipo_operacao") == "COMPRA":
                lanc["tipo_operacao"] = "PAGAMENTO"
            elif registro["credito"] > 0 and lanc.get("tipo_operacao") == "PAGAMENTO":
                lanc["tipo_operacao"] = "COMPRA"
            corrigidos += 1

    if corrigidos:
        print(f"   📐 {corrigidos} lançamento(s) reatribuído(s) pela coluna do PDF")

    return lancamentos


# ─────────────────────────────────────────────────────────────────────────────
# Parser determinístico para blocos com colunas alinhadas
#
# Quando o bloco traz o header da tabela, a posição de cada número resolve tudo
# sem ambiguidade e não há motivo para envolver a IA — que decidia débito x
# crédito por palavra-chave e variava entre execuções (o mesmo PDF chegou a
# produzir "a pagar" de R$ 2.758,20 e R$ 33.416,50).
#
# O parser só assume o bloco quando CONSEGUE SE PROVAR: a soma dos lançamentos
# extraídos precisa fechar com o "Total da conta" impresso no próprio PDF. Se
# não fechar, devolve None e o fluxo cai na IA como antes.
# ─────────────────────────────────────────────────────────────────────────────

_RE_VALOR_COM_TIPO = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})([CD])?")
_RE_TOTAL_CONTA = re.compile(
    r"Total da conta:\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s+(\d{1,3}(?:\.\d{3})*,\d{2})"
)
_RE_SALDO_ANTERIOR = re.compile(
    r"SALDO ANTERIOR.*?(\d{1,3}(?:\.\d{3})*,\d{2})([CD])?", re.IGNORECASE
)


def _decimal_br(texto: str) -> Decimal:
    return Decimal(texto.replace(".", "").replace(",", "."))


def _ler_linha_lancamento(linha: str, colunas: Dict) -> Optional[Dict]:
    """Extrai um lançamento completo de uma linha alinhada."""
    m = re.match(r"^\s*(\d{2}/\d{2}/\d{4})\s+(\S+)", linha)
    if not m:
        return None

    try:
        data = datetime.strptime(m.group(1), "%d/%m/%Y")
    except ValueError:
        return None

    valores: Dict[str, Decimal] = {}
    saldo_tipo = ""
    for achado in _RE_VALOR_COM_TIPO.finditer(linha):
        if achado.start() < m.end(2):
            continue
        coluna = _coluna_do_valor(achado.end(1), colunas)
        if not coluna:
            continue
        valores[coluna] = valores.get(coluna, Decimal("0")) + _decimal_br(achado.group(1))
        if coluna.startswith("saldo") and achado.group(2):
            saldo_tipo = achado.group(2)

    debito = valores.get("debito", Decimal("0"))
    credito = valores.get("credito", Decimal("0"))
    if debito == 0 and credito == 0:
        return None

    inicio_hist = colunas["historico"] if colunas["historico"] is not None else m.end(2)
    fim_hist = colunas["cta"] if colunas["cta"] is not None else colunas["inicio_debito"]
    historico = linha[inicio_hist:fim_hist].strip() if len(linha) > inicio_hist else ""

    conta_partida = None
    if colunas["cta"] is not None:
        trecho = linha[colunas["cta"]:colunas["inicio_debito"]]
        achado_cta = re.search(r"\d+", trecho)
        if achado_cta:
            conta_partida = achado_cta.group()

    saldo = next(
        (v for k, v in valores.items() if k.startswith("saldo")), Decimal("0")
    )

    return {
        "data_lancamento": data,
        "lote": m.group(2),
        "historico": historico,
        "conta_partida": conta_partida,
        "valor_debito": debito,
        "valor_credito": credito,
        "saldo_apos_lancamento": saldo,
        "saldo_tipo": saldo_tipo,
        "tipo_operacao": "PAGAMENTO" if debito > 0 else "COMPRA",
        "numero_nf": None,
        "cnpj_historico": None,
        "classificacao_incerta": False,
        "classificado_por_ia": False,
    }


def _e_continuacao_de_historico(linha: str, colunas: Dict) -> bool:
    """Linha sem data e sem valores nas colunas monetárias: sobra do histórico."""
    if not linha.strip() or re.match(r"^\s*\d{2}/\d{2}/\d{4}", linha):
        return False
    if "Total da conta" in linha or linha.strip().startswith("Conta:"):
        return False
    for achado in _RE_VALOR_COM_TIPO.finditer(linha):
        if _coluna_do_valor(achado.end(1), colunas):
            return False
    return True


def parsear_bloco_deterministico(linhas: List[str]) -> Optional[Dict]:
    """
    Lê um bloco de fornecedor sem IA, usando o alinhamento das colunas.

    Devolve None quando não se aplica (sem header, sem totais declarados) ou
    quando o resultado não fecha com o "Total da conta" do PDF — nesses casos o
    chamador cai na IA. Só assume o bloco quando pode provar que acertou.
    """
    colunas = _colunas_do_header(linhas)
    if not colunas:
        return None

    texto = "\n".join(linhas)
    m_total = _RE_TOTAL_CONTA.search(texto)
    if not m_total:
        return None  # sem gabarito não há como se autovalidar
    total_debito = _decimal_br(m_total.group(1))
    total_credito = _decimal_br(m_total.group(2))

    lancamentos: List[Dict] = []
    for linha in linhas:
        lanc = _ler_linha_lancamento(linha, colunas)
        if lanc:
            lancamentos.append(lanc)
        elif lancamentos and _e_continuacao_de_historico(linha, colunas):
            sobra = linha.strip()
            if sobra and not sobra.isdigit():
                lancamentos[-1]["historico"] = f"{lancamentos[-1]['historico']} {sobra}".strip()

    if not lancamentos:
        # Conta aberta no plano mas sem movimento no período: é resultado
        # legítimo, não falha de leitura. Só aceita quando o PDF declara
        # totais zerados E não há nenhuma linha de lançamento no bloco — se
        # houver data e nada foi lido, aí sim é falha, e vai para a IA.
        tem_linha_de_lancamento = any(
            re.match(r"^\s*\d{2}/\d{2}/\d{4}", linha) for linha in linhas
        )
        if total_debito == 0 and total_credito == 0 and not tem_linha_de_lancamento:
            saldo_ant = Decimal("0")
            saldo_ant_tipo = ""
            m_sa = _RE_SALDO_ANTERIOR.search(texto)
            if m_sa:
                saldo_ant = _decimal_br(m_sa.group(1))
                saldo_ant_tipo = m_sa.group(2) or ""
            print("   ⚪ Conta sem movimento no período — registrada com saldo zero.")
            return {
                "saldo_anterior": saldo_ant,
                "saldo_anterior_tipo": saldo_ant_tipo,
                "total_debito": Decimal("0"),
                "total_credito": Decimal("0"),
                "lancamentos": [],
                "sem_movimento": True,
            }
        return None

    soma_debito = sum(l["valor_debito"] for l in lancamentos)
    soma_credito = sum(l["valor_credito"] for l in lancamentos)
    if (abs(soma_debito - total_debito) > Decimal("0.01")
            or abs(soma_credito - total_credito) > Decimal("0.01")):
        print(
            f"   ⚖️ Determinístico não fechou (D {soma_debito} vs {total_debito}, "
            f"C {soma_credito} vs {total_credito}) — usando IA."
        )
        return None

    for lanc in lancamentos:
        lanc["historico"] = re.sub(r"\s{2,}", " ", lanc["historico"]).strip()
        lanc["numero_nf"] = extrair_numero_nf(lanc["historico"])
        lanc["cnpj_historico"] = extrair_cnpj(lanc["historico"])

    saldo_anterior = Decimal("0")
    saldo_anterior_tipo = ""
    m_saldo = _RE_SALDO_ANTERIOR.search(texto)
    if m_saldo:
        saldo_anterior = _decimal_br(m_saldo.group(1))
        saldo_anterior_tipo = m_saldo.group(2) or ""

    print(f"   ⚙️ Bloco lido sem IA: {len(lancamentos)} lançamentos, totais conferem.")

    return {
        "saldo_anterior": saldo_anterior,
        "saldo_anterior_tipo": saldo_anterior_tipo,
        "total_debito": total_debito,
        "total_credito": total_credito,
        "lancamentos": lancamentos,
    }


def _identificar_fornecedor(linhas: List[str]) -> Optional[Tuple[str, str, str]]:
    """Extrai (codigo_conta, conta_contabil, nome) da linha 'Conta:' do bloco."""
    for linha in linhas:
        m = re.match(r"Conta:\s*(\d+)\s*(?:-\s*)?([\d.]+)\s+(.+)$", linha.strip())
        if m:
            return m.group(1), m.group(2), m.group(3).strip()

    # 'Conta:' sem código — cai para o nome que vier logo abaixo
    nome_fornecedor = None
    for i, linha in enumerate(linhas):
        if re.match(r"Conta:\s*$", linha.strip()):
            for j in range(i + 1, min(i + 6, len(linhas))):
                candidata = re.sub(r'\s+', ' ', linhas[j].strip())
                if _parece_nome_fornecedor(candidata):
                    nome_fornecedor = candidata
                    break
            break

    if not nome_fornecedor:
        return None

    h = hashlib.md5(nome_fornecedor.upper().encode()).hexdigest()
    print(f"⚠️ Conta: vazia — usando nome '{nome_fornecedor}' com código gerado {h[:6]}")
    return h[:6], "0.0.0.00.0000", nome_fornecedor


def _construir_fornecedor_deterministico(dados: Dict, linhas: List[str]) -> Optional[Dict]:
    """Monta o fornecedor a partir do parse determinístico, sem passar pela IA."""
    identidade = _identificar_fornecedor(linhas)
    if not identidade:
        return None
    codigo_conta, conta_contabil, nome_fornecedor = identidade

    return {
        "codigo_conta": codigo_conta,
        "conta_contabil": conta_contabil,
        "nome_fornecedor": nome_fornecedor,
        "saldo_anterior": dados["saldo_anterior"],
        "saldo_anterior_tipo": dados["saldo_anterior_tipo"],
        "total_debito": dados["total_debito"],
        "total_credito": dados["total_credito"],
        "lancamentos": dados["lancamentos"],
        "conciliacao_ia": [],
        "resumo_ia": {},
        "validacao_ia": {},
    }


def _recuperar_lancamentos_ocultos(
    lancamentos: List[Dict],
    linhas_bloco: List[str],
    total_debito_declarado: Decimal = Decimal("0"),
    total_credito_declarado: Decimal = Decimal("0"),
) -> List[Dict]:
    """
    Detecta lançamentos ocultos analisando saltos de saldo entre entradas consecutivas.

    Só atua quando há de fato algo faltando: se os totais declarados em
    "Total da conta" já fecham com a soma dos lançamentos parseados, nada está
    oculto e fabricar entradas aqui só produziria valores falsos. Essa guarda
    importa porque os lançamentos são reordenados por data na consolidação —
    em contas cujo saldo oscila (tributos, FGTS), a reordenação quebra a cadeia
    de saldos e a análise de gap enxerga lacunas que não existem.
    """
    TOLERANCIA = Decimal("0.05")
    texto_bloco = "\n".join(linhas_bloco).upper()

    if total_debito_declarado > 0 or total_credito_declarado > 0:
        soma_debito = sum(Decimal(str(l.get("valor_debito") or 0)) for l in lancamentos)
        soma_credito = sum(Decimal(str(l.get("valor_credito") or 0)) for l in lancamentos)
        debito_fecha = abs(soma_debito - total_debito_declarado) <= TOLERANCIA
        credito_fecha = abs(soma_credito - total_credito_declarado) <= TOLERANCIA
        if debito_fecha and credito_fecha:
            print("   ✅ Totais declarados fecham com os lançamentos — sem recuperação.")
            return lancamentos

    nfs_atribuidas = {l.get("numero_nf") for l in lancamentos if l.get("numero_nf")}
    _seen: set = set()
    nfs_unicas: List[str] = []
    for nf in re.findall(r'N[ÚU]MERO\s+(\d{6,7})', texto_bloco):
        if nf not in _seen:
            _seen.add(nf)
            nfs_unicas.append(nf)
    nfs_livres = [nf for nf in nfs_unicas if nf not in nfs_atribuidas]
    nf_ptr = 0

    resultado: List[Dict] = []

    for i, lanc in enumerate(lancamentos):
        resultado.append(lanc)

        if i + 1 >= len(lancamentos):
            break

        prox = lancamentos[i + 1]

        saldo_atual = Decimal(str(lanc.get("saldo_apos_lancamento") or 0))
        saldo_prox  = Decimal(str(prox.get("saldo_apos_lancamento") or 0))
        vc_prox     = Decimal(str(prox.get("valor_credito") or 0))
        vd_prox     = Decimal(str(prox.get("valor_debito") or 0))

        if saldo_atual == 0 or saldo_prox == 0:
            continue

        gap = saldo_prox - saldo_atual - vc_prox + vd_prox

        if abs(gap) < TOLERANCIA:
            continue

        data_sint = lanc.get("data_lancamento") or prox.get("data_lancamento")

        if gap > 0:
            numero_nf = nfs_livres[nf_ptr] if nf_ptr < len(nfs_livres) else None
            nf_ptr += 1
            if numero_nf:
                nfs_atribuidas.add(numero_nf)
            sintetico = {
                "data_lancamento": data_sint,
                "lote": "",
                "historico": f"COMPRA NF {numero_nf} (RECUPERADO)" if numero_nf else "COMPRA (RECUPERADO)",
                "conta_partida": None,
                "valor_debito": Decimal("0"),
                "valor_credito": gap,
                "saldo_apos_lancamento": saldo_atual + gap,
                "saldo_tipo": "C",
                "tipo_operacao": "COMPRA",
                "numero_nf": numero_nf,
                "cnpj_historico": None,
                "classificacao_incerta": False,
                "classificado_por_ia": True,
                "sintetico": True,
            }
            print(f"🔧 Recuperado: COMPRA NF={numero_nf or '?'} R$ {float(gap):.2f}")
        else:
            valor_pag = abs(gap)
            if "SISPAG" in texto_bloco:
                hist = "SISPAG (RECUPERADO)"
            elif "BOLETO" in texto_bloco:
                hist = "BOLETO (RECUPERADO)"
            else:
                hist = "PAGAMENTO (RECUPERADO)"
            sintetico = {
                "data_lancamento": data_sint,
                "lote": "",
                "historico": hist,
                "conta_partida": None,
                "valor_debito": valor_pag,
                "valor_credito": Decimal("0"),
                "saldo_apos_lancamento": saldo_atual - valor_pag,
                "saldo_tipo": "C",
                "tipo_operacao": "PAGAMENTO",
                "numero_nf": None,
                "cnpj_historico": None,
                "classificacao_incerta": False,
                "classificado_por_ia": True,
                "sintetico": True,
            }
            print(f"🔧 Recuperado: PAGAMENTO R$ {float(valor_pag):.2f}")

        resultado.append(sintetico)

    recuperados = len(resultado) - len(lancamentos)
    if recuperados > 0:
        print(f"🔧 Total: {recuperados} lançamentos ocultos recuperados por análise de saldo")
        print(f"   ⚠️ Lançamentos fabricados — fornecedor será marcado com divergência.")

    return resultado


def _parece_nome_fornecedor(linha: str) -> bool:
    """Heurística: linha limpa de nome de fornecedor."""
    linha = linha.strip()
    if not linha or len(linha) < 4:
        return False
    if re.search(r'(?:\d[A-Z]){3}|(?:[A-Z]\d){3}', linha):
        return False
    if re.match(r'^[\d.,/\s]+$', linha):
        return False
    return sum(c.isalpha() for c in linha) >= 4


def _construir_fornecedor_de_ia(dados_ia: dict, linhas: List[str]) -> Optional[Dict]:
    """
    Combina o header do fornecedor (extraído por regex da linha 'Conta:')
    com os dados financeiros retornados pela IA.
    """
    codigo_conta = None
    conta_contabil = None
    nome_fornecedor = None

    identidade = _identificar_fornecedor(linhas)
    if not identidade:
        return None
    codigo_conta, conta_contabil, nome_fornecedor = identidade

    lancamentos = []
    for lanc_ia in dados_ia.get("lancamentos", []):
        normalizado = _normalizar_lancamento_ia(lanc_ia)
        if normalizado:
            lancamentos.append(normalizado)

    if not lancamentos:
        brutos = dados_ia.get("lancamentos", [])
        print(
            f"⚠️ IA: {len(brutos)} lançamentos brutos mas nenhum normalizou. "
            f"Primeiro: {brutos[0] if brutos else 'vazio'}"
        )
        return None

    lancamentos = _corrigir_colunas_por_posicao(lancamentos, linhas)

    lancamentos = _recuperar_lancamentos_ocultos(
        lancamentos,
        linhas,
        total_debito_declarado=_ia_decimal(dados_ia.get("total_debito")),
        total_credito_declarado=_ia_decimal(dados_ia.get("total_credito")),
    )

    conciliacao_ia = dados_ia.get("conciliacao") or []
    resumo_ia      = dados_ia.get("resumo") or {}
    validacao_ia   = dados_ia.get("validacao") or {}

    if validacao_ia.get("observacoes"):
        for obs in validacao_ia["observacoes"]:
            logger.warning("📋 Validação IA: %s", obs)

    print(
        f"✅ IA parseou bloco: {len(lancamentos)} lançamentos, "
        f"débito={float(dados_ia.get('total_debito') or 0):.2f}, "
        f"crédito={float(dados_ia.get('total_credito') or 0):.2f}, "
        f"conciliação: {len(conciliacao_ia)} NFs, "
        f"saldo_confere={validacao_ia.get('saldo_confere', '?')}"
    )

    return {
        "codigo_conta": codigo_conta,
        "conta_contabil": conta_contabil,
        "nome_fornecedor": nome_fornecedor,
        "saldo_anterior": _ia_decimal(dados_ia.get("saldo_anterior")),
        "saldo_anterior_tipo": str(dados_ia.get("saldo_anterior_tipo") or ""),
        "total_debito": _ia_decimal(dados_ia.get("total_debito")),
        "total_credito": _ia_decimal(dados_ia.get("total_credito")),
        "lancamentos": lancamentos,
        "conciliacao_ia": conciliacao_ia,
        "resumo_ia": resumo_ia,
        "validacao_ia": validacao_ia,
    }


def _renderizar_paginas_pdf(arquivo_bytes: bytes, page_indices: List[int]) -> List[bytes]:
    """Renderiza páginas do PDF como PNG bytes usando PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=arquivo_bytes, filetype="pdf")
        resultado = []
        for idx in page_indices:
            if 0 <= idx < len(doc):
                pag = doc[idx]
                pix = pag.get_pixmap(dpi=150)
                resultado.append(pix.tobytes("png"))
        doc.close()
        return resultado
    except ImportError:
        print("⚠️ pymupdf não instalado — Vision desativado. Execute: pip install pymupdf")
        return []
    except Exception as e:
        print(f"⚠️ Falha ao renderizar páginas {page_indices}: {e}")
        return []


def parsear_arquivo_razao(arquivo_bytes: bytes) -> Dict:
    """
    Função principal: parseia todo o arquivo PDF usando IA.
    Pipeline: Vision (primário) → texto gpt-4o-mini (fallback).
    """
    from src.domain.concilpro.ai_classifier import (
        parsear_bloco_fornecedor_ia,
        parsear_bloco_fornecedor_ia_visao,
    )

    hash_arquivo = calcular_hash_arquivo(arquivo_bytes)
    formato = detectar_formato_arquivo(arquivo_bytes)
    print(f"🔍 Formato detectado: {formato}")

    if formato == 'XLSX':
        # Planilha declara o que o PDF obriga a inferir: célula tipada, coluna
        # nomeada e sem paginação. Caminho determinístico, sem IA.
        from src.domain.concilpro.planilha import parsear_planilha_razao
        return parsear_planilha_razao(arquivo_bytes)

    if formato != 'PDF':
        raise ValueError(
            f"Formato '{formato}' não suportado. "
            "Envie o Razão em PDF ou em planilha XLSX."
        )

    print("📖 Extraindo texto do PDF...")

    paginas_dados: List[Tuple] = []
    try:
        with pdfplumber.open(BytesIO(arquivo_bytes)) as pdf:
            total_paginas = len(pdf.pages)
            print(f"📄 PDF detectado: {total_paginas} páginas")
            for i, texto in enumerate(_extrair_paginas(pdf)):
                paginas_dados.append((texto, i))
                if (i + 1) % 10 == 0:
                    print(f"   Processadas {i+1}/{total_paginas} páginas...")
            print(f"✅ Extração concluída: {len(paginas_dados)} páginas com texto")
    except Exception as e:
        raise ValueError(f"Erro ao extrair texto do PDF: {str(e)}")

    texto_completo = "\n\n".join(t for t, _ in paginas_dados)
    if not texto_completo or len(texto_completo) < 20:
        logger.warning(
            "⚠️ PDF com texto muito escasso (%d chars) — Vision tentará mesmo assim",
            len(texto_completo or ""),
        )
    print(f"✅ Texto extraído: {len(texto_completo)} caracteres")

    linha_para_pagina: Dict[int, int] = {}
    ln = 0
    for pg_idx, (texto, _) in enumerate(paginas_dados):
        for _ in texto.split('\n'):
            linha_para_pagina[ln] = pg_idx
            ln += 1
        if pg_idx < len(paginas_dados) - 1:
            ln += 1

    fornecedores: List[Dict] = []
    blocos_ia_texto = 0
    blocos_ia_visao = 0
    blocos_deterministicos = [0]  # contador mutável no closure

    linhas = texto_completo.split('\n')

    def _tem_valores(lancamentos: list) -> bool:
        return any(
            float(l.get("valor_credito") or 0) + float(l.get("valor_debito") or 0) > 0
            for l in lancamentos
        )

    total_blocos = sum(1 for l in linhas if l.strip().startswith('Conta:'))
    bloco_num = [0]  # contador mutável no closure

    # O header da tabela ("Data Lote Histórico Cta.C.Part. Débito Crédito ...")
    # aparece antes do primeiro "Conta:" e por isso ficaria fora dos blocos.
    # Como a extração preserva o alinhamento, é a posição da coluna que
    # distingue Débito de Crédito — sem o header a IA perde essa referência.
    linha_header = next(
        (l for l in linhas if 'Débito' in l and 'Crédito' in l and 'Data' in l),
        "",
    )
    if linha_header:
        print(f"   📐 Header de colunas localizado — será enviado com cada bloco.")

    def _processar_bloco(linhas_bloco: List[str], linha_inicio: int) -> None:
        nonlocal blocos_ia_texto, blocos_ia_visao
        if not linhas_bloco:
            return

        bloco_num[0] += 1
        if linha_header and linha_header not in linhas_bloco:
            linhas_bloco = [linha_header] + linhas_bloco
        bloco_texto_raw = '\n'.join(linhas_bloco)
        bloco_texto = _preprocessar_bloco(bloco_texto_raw)
        if bloco_texto != bloco_texto_raw:
            print(f"   🧹 Pré-processamento removeu repetições de histórico.")
        print(f"\n[{bloco_num[0]}/{total_blocos}] Processando bloco ({len(linhas_bloco)} linhas)...")

        # ── Tentativa 0: parser determinístico (sem IA, sem custo, sem variação) ──
        # Só assume o bloco se a soma dos lançamentos fechar com o "Total da
        # conta" impresso no PDF; caso contrário devolve None e segue para a IA.
        linhas_preprocessadas = bloco_texto.split('\n')
        dados_det = parsear_bloco_deterministico(linhas_preprocessadas)
        # conta sem movimento não tem valores, e ainda assim é resultado válido
        if dados_det and (_tem_valores(dados_det["lancamentos"])
                          or dados_det.get("sem_movimento")):
            fornecedor = _construir_fornecedor_deterministico(dados_det, linhas_preprocessadas)
            if fornecedor:
                fornecedores.append(fornecedor)
                blocos_deterministicos[0] += 1
                return

        # ── Tentativa 1: IA com texto (GPT-4o-mini — rápido, barato ~5s/bloco) ──
        dados_ia = parsear_bloco_fornecedor_ia(bloco_texto)
        if dados_ia and _tem_valores(dados_ia.get("lancamentos", [])):
            fornecedor = _construir_fornecedor_de_ia(dados_ia, linhas_bloco)
            if fornecedor:
                fornecedores.append(fornecedor)
                blocos_ia_texto += 1
                return

        # ── Tentativa 2: Vision (GPT-4o — mais lento, só se texto falhou) ──
        page_indices = sorted({
            linha_para_pagina[i]
            for i in range(linha_inicio, linha_inicio + len(linhas_bloco))
            if i in linha_para_pagina
        })
        if page_indices:
            print(f"   👁️ Fallback Vision (págs {page_indices})...")
            png_list = _renderizar_paginas_pdf(arquivo_bytes, page_indices)
            if png_list:
                dados_visao = parsear_bloco_fornecedor_ia_visao(png_list, bloco_texto)
                if dados_visao and _tem_valores(dados_visao.get("lancamentos", [])):
                    fornecedor = _construir_fornecedor_de_ia(dados_visao, linhas_bloco)
                    if fornecedor:
                        fornecedores.append(fornecedor)
                        blocos_ia_visao += 1
                        return

        logger.warning(
            "⚠️ Bloco descartado (texto+Vision falharam): %d linhas | primeiras: %s",
            len(linhas_bloco), linhas_bloco[:3],
        )

    def _codigo_da_linha_conta(linha: str) -> Optional[str]:
        m = re.match(r"Conta:\s*(\d+)", linha.strip())
        return m.group(1) if m else None

    fornecedor_atual: List[str] = []
    linha_inicio_atual = 0
    codigo_atual: Optional[str] = None

    for i, linha in enumerate(linhas):
        # NÃO fazer strip aqui: o recuo é a coluna, e a coluna define se um
        # valor é Débito ou Crédito. Quem precisa da versão limpa faz o strip.
        if linha.lstrip().startswith('Conta:'):
            codigo = _codigo_da_linha_conta(linha)
            # Quando a conta atravessa a quebra de página, o razão repete o
            # cabeçalho `Conta:` no topo da página seguinte. Não é um novo
            # fornecedor: fechar o bloco aqui produziria uma visão parcial —
            # metade dos lançamentos de um lado e o "Total da conta" do outro.
            if codigo is not None and codigo == codigo_atual:
                continue
            _processar_bloco(fornecedor_atual, linha_inicio_atual)
            fornecedor_atual = []
            linha_inicio_atual = i
            codigo_atual = codigo
        fornecedor_atual.append(linha)
    _processar_bloco(fornecedor_atual, linha_inicio_atual)

    print(f"📋 Total de registros antes da consolidação: {len(fornecedores)}")
    if len(fornecedores) > 0:
        fornecedores = consolidar_fornecedores_duplicados(fornecedores)

    empresa = ""
    cnpj = ""
    periodo_inicio = None
    periodo_fim = None

    match_periodo = re.search(r'(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})', texto_completo)
    if match_periodo:
        periodo_inicio = parse_data(match_periodo.group(1))
        periodo_fim = parse_data(match_periodo.group(2))

    match_empresa = re.search(r'Empresa:\s*(.+?)(?:\s+Folha:|\n)', texto_completo)
    if match_empresa:
        empresa = match_empresa.group(1).strip()

    match_cnpj = re.search(r'C\.N\.P\.J\.:\s*([\d./-]+)', texto_completo)
    if match_cnpj:
        cnpj = match_cnpj.group(1).strip()

    print(
        f"✅ Processamento concluído: {len(fornecedores)} fornecedores "
        f"(determinístico: {blocos_deterministicos[0]} | texto: {blocos_ia_texto} "
        f"| Vision: {blocos_ia_visao})"
    )

    return {
        'hash_arquivo': hash_arquivo,
        'empresa': empresa,
        'cnpj': cnpj,
        'periodo_inicio': periodo_inicio,
        'periodo_fim': periodo_fim,
        'total_fornecedores': len(fornecedores),
        'total_lancamentos': sum(len(f.get('lancamentos', [])) for f in fornecedores),
        'fornecedores': fornecedores,
    }
