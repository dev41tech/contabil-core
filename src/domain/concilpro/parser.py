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

logger = logging.getLogger(__name__)


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


def extrair_texto_pdf(arquivo_bytes: bytes) -> str:
    """
    Extrai texto de um PDF usando pdfplumber.
    """
    texto_completo = []

    try:
        with pdfplumber.open(BytesIO(arquivo_bytes)) as pdf:
            total_paginas = len(pdf.pages)
            print(f"📄 PDF detectado: {total_paginas} páginas")

            for i, pagina in enumerate(pdf.pages, 1):
                texto_layout = pagina.extract_text(layout=True) or ""
                texto_words  = _extrair_pagina_por_palavras(pagina)
                texto = texto_layout if len(texto_layout) >= len(texto_words) else texto_words
                if not texto:
                    texto = pagina.extract_text() or ""
                if texto:
                    texto_completo.append(texto)

                if i % 10 == 0:
                    print(f"   Processadas {i}/{total_paginas} páginas...")

            print(f"✅ Extração concluída: {len(texto_completo)} páginas com texto")

    except Exception as e:
        raise ValueError(f"Erro ao extrair texto do PDF: {str(e)}")

    return "\n\n".join(texto_completo)


def detectar_formato_arquivo(arquivo_bytes: bytes) -> str:
    """Detecta se o arquivo é PDF ou ZIP."""
    if arquivo_bytes[:4] == b'%PDF':
        return 'PDF'
    elif arquivo_bytes[:2] == b'PK':
        return 'ZIP'
    else:
        if zipfile.is_zipfile(BytesIO(arquivo_bytes)):
            return 'ZIP'
        else:
            return 'PDF'


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
            fornecedores_consolidados.append(lista_forn[0])
        else:
            print(f"   🔧 Consolidando {len(lista_forn)} registros do código {codigo} - {lista_forn[0]['nome_fornecedor']}")

            consolidado = lista_forn[0].copy()

            todos_lancamentos = []
            for forn in lista_forn:
                todos_lancamentos.extend(forn.get('lancamentos', []))

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


def _recuperar_lancamentos_ocultos(lancamentos: List[Dict], linhas_bloco: List[str]) -> List[Dict]:
    """
    Detecta lançamentos ocultos analisando saltos de saldo entre entradas consecutivas.
    """
    TOLERANCIA = Decimal("0.05")
    texto_bloco = "\n".join(linhas_bloco).upper()

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
            }
            print(f"🔧 Recuperado: PAGAMENTO R$ {float(valor_pag):.2f}")

        resultado.append(sintetico)

    recuperados = len(resultado) - len(lancamentos)
    if recuperados > 0:
        print(f"🔧 Total: {recuperados} lançamentos ocultos recuperados por análise de saldo")

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

    for linha in linhas:
        m = re.match(r"Conta:\s*(\d+)\s*(?:-\s*)?([\d.]+)\s+(.+)$", linha.strip())
        if m:
            codigo_conta = m.group(1)
            conta_contabil = m.group(2)
            nome_fornecedor = m.group(3).strip()
            break

    if not codigo_conta:
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
        codigo_conta = h[:6]
        conta_contabil = "0.0.0.00.0000"
        print(f"⚠️ Conta: vazia — usando nome '{nome_fornecedor}' com código gerado {codigo_conta}")

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

    lancamentos = _recuperar_lancamentos_ocultos(lancamentos, linhas)

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

    if formato != 'PDF':
        raise ValueError(
            f"Formato '{formato}' não suportado nesta versão. "
            "Este parser funciona apenas com arquivos PDF."
        )

    print("📖 Extraindo texto do PDF...")

    paginas_dados: List[Tuple] = []
    try:
        with pdfplumber.open(BytesIO(arquivo_bytes)) as pdf:
            total_paginas = len(pdf.pages)
            print(f"📄 PDF detectado: {total_paginas} páginas")
            for i, pagina in enumerate(pdf.pages):
                texto_layout = pagina.extract_text(layout=True) or ""
                texto_words  = _extrair_pagina_por_palavras(pagina)
                texto = texto_layout if len(texto_layout) >= len(texto_words) else texto_words
                if not texto:
                    texto = pagina.extract_text() or ""
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

    linhas = texto_completo.split('\n')

    def _tem_valores(lancamentos: list) -> bool:
        return any(
            float(l.get("valor_credito") or 0) + float(l.get("valor_debito") or 0) > 0
            for l in lancamentos
        )

    total_blocos = sum(1 for l in linhas if l.strip().startswith('Conta:'))
    bloco_num = [0]  # contador mutável no closure

    def _processar_bloco(linhas_bloco: List[str], linha_inicio: int) -> None:
        nonlocal blocos_ia_texto, blocos_ia_visao
        if not linhas_bloco:
            return

        bloco_num[0] += 1
        bloco_texto = '\n'.join(linhas_bloco)
        print(f"\n[{bloco_num[0]}/{total_blocos}] Processando bloco ({len(linhas_bloco)} linhas)...")

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

    fornecedor_atual: List[str] = []
    linha_inicio_atual = 0
    for i, linha in enumerate(linhas):
        linha = linha.strip()
        if linha.startswith('Conta:'):
            _processar_bloco(fornecedor_atual, linha_inicio_atual)
            fornecedor_atual = []
            linha_inicio_atual = i
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
        f"(Vision: {blocos_ia_visao} | texto: {blocos_ia_texto})"
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
