"""
Pós-Processamento: Consolidação de Lançamentos
===============================================

PROBLEMA:
Quando uma NF é lançada em múltiplas contas de custo (CPCs diferentes),
o sistema contábil cria vários lançamentos para a mesma NF na mesma data.

SOLUÇÃO:
Após o parser extrair os lançamentos, consolidar por:
  - numero_nf + série + data_lancamento + tipo_operacao

IMPORTANTE:
- COMPRAS: Consolidar se mesmo número e série de NF + mesma data
- PAGAMENTOS: NÃO consolidar (podem haver múltiplos pagamentos no mesmo dia)
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Dict, List


_SERIE_NF_RE = re.compile(
    r"\bS[ÉE]R(?:IE|\.)?\s*(?:N[ºO]\s*)?[:#-]?\s*([A-Z0-9][A-Z0-9./-]*)",
    re.IGNORECASE,
)


def _serie_da_nota(lancamento: Dict) -> str:
    explicita = lancamento.get("serie_nf") or lancamento.get("serie")
    if explicita is not None and str(explicita).strip():
        return str(explicita).strip().upper()

    historico = str(lancamento.get("historico") or "")
    encontrada = _SERIE_NF_RE.search(historico)
    return encontrada.group(1).upper() if encontrada else ""


def consolidar_lancamentos_fornecedor(lancamentos: List[Dict]) -> List[Dict]:
    """
    Consolida compras com o mesmo número, série e data da NF.
    """
    compras = [l for l in lancamentos if l.get('tipo_operacao') == 'COMPRA']
    outros = [l for l in lancamentos if l.get('tipo_operacao') != 'COMPRA']

    grupos = {}

    for lanc in compras:
        nf = lanc.get('numero_nf')
        serie = _serie_da_nota(lanc)
        data = lanc.get('data_lancamento')

        if isinstance(data, datetime):
            data_str = data.strftime('%Y-%m-%d')
        else:
            data_str = str(data)

        if not nf or nf == '':
            chave = ("SEM_NF", id(lanc))
        else:
            chave = (str(nf), str(serie).strip(), data_str)

        if chave not in grupos:
            grupos[chave] = []

        grupos[chave].append(lanc)

    compras_consolidadas = []

    for chave, grupo in grupos.items():
        if len(grupo) == 1:
            compras_consolidadas.append(grupo[0])
        else:
            primeiro = grupo[0]

            valor_total = sum(
                Decimal(str(l.get('valor_credito', 0)))
                for l in grupo
            )

            consolidado = primeiro.copy()
            consolidado['valor_credito'] = valor_total
            consolidado['consolidado'] = True
            consolidado['lancamentos_originais'] = len(grupo)

            compras_consolidadas.append(consolidado)

    resultado = compras_consolidadas + outros
    resultado.sort(key=lambda x: x.get('data_lancamento'))

    return resultado


def consolidar_todos_fornecedores(dados_parser: Dict) -> Dict:
    """
    Aplica consolidação em todos os fornecedores extraídos pelo parser.
    """
    resultado = dados_parser.copy()

    print("🔧 Consolidando lançamentos...")

    for fornecedor in resultado['fornecedores']:
        lancamentos_originais = fornecedor['lancamentos']
        lancamentos_consolidados = consolidar_lancamentos_fornecedor(lancamentos_originais)

        fornecedor['lancamentos'] = lancamentos_consolidados

        parsed_credito = Decimal(str(fornecedor.get('total_credito') or 0))
        parsed_debito  = Decimal(str(fornecedor.get('total_debito')  or 0))

        if parsed_credito == 0:
            parsed_credito = sum(
                Decimal(str(l.get('valor_credito', 0))) for l in lancamentos_consolidados
            )
            fornecedor['total_credito'] = float(parsed_credito)

        if parsed_debito == 0:
            parsed_debito = sum(
                Decimal(str(l.get('valor_debito', 0))) for l in lancamentos_consolidados
            )
            fornecedor['total_debito'] = float(parsed_debito)

        qtd_original = len(lancamentos_originais)
        qtd_consolidado = len(lancamentos_consolidados)

        if qtd_consolidado < qtd_original:
            print(f"   ✅ {fornecedor['nome_fornecedor'][:40]}: "
                  f"{qtd_original} → {qtd_consolidado} lançamentos "
                  f"({qtd_original - qtd_consolidado} consolidados)")

    return resultado
