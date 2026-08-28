"""Extrato gerado pelo Omie.CASH — duas variantes de coluna de valor.

Não é o extrato de um banco, é o de um sistema: o Omie exporta neste formato
para contas de bancos diferentes. O arquivo que abriu a segunda variante se
chama "Extrato de Sicredi" e tem exatamente o mesmo esqueleto do FitBank. Por
isso o adaptador é escolhido pela ASSINATURA e não pela sigla do banco — a
agência pode estar cadastrada como SICREDI e mesmo assim cair aqui.

**Variante A — duas colunas** (FitBank):

    Situação   Data   Cliente ou Fornecedor   Categoria       Entradas  Saídas  Saldo
               31/05  SALDO ANTERIOR                                            19,40
    Conciliado 01/06  QUEBRA PRECO            Clientes - ...  3.570,00  -       3.589,40
    Conciliado 01/06  tar.20260601.163923     Tarifas Banc.   -         1,99    3.587,41

A coluna vazia vem escrita como `-`, não em branco, e é a posição que diz o
sinal: valor em Entradas é crédito, em Saídas é débito.

**Variante B — uma coluna** (o "Extrato de Sicredi"):

    Situação   Data   Cliente ou Fornecedor   Categoria       Valor      Saldo
               30/06  SALDO ANTERIOR                                     783.587,34
    Conciliado 01/07  EXEMPLO ALIMENTOS LTDA  Clientes - ...  2.024,50   785.611,84

Aqui não há coluna que informe o sinal, então ele só pode vir do próprio número.

> **O que não deu para verificar:** a amostra da variante B é de um mês só de
> créditos — não há uma única linha de débito nela. Então a convenção de sinal
> para saída está assumida (menos no número), não observada. Isso é deliberado e
> seguro: se o Omie escrever débito sem sinal, a cadeia de saldos não fecha e o
> arquivo é RECUSADO, em vez de entrar com o sinal trocado. Quando aparecer um
> extrato dessa variante com débito, vale conferir e apagar este aviso.

Comum às duas: a data não tem ano (`01/06`) — ele sai do cabeçalho "Período de
01/06/2026 até 30/06/2026", porque usar o ano corrente erraria todo extrato de
dezembro processado em janeiro. E as duas trazem `SALDO ANTERIOR` mais saldo em
toda linha, então a cadeia fecha inteira.
"""

from __future__ import annotations

import re
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"FITBANK", "OMIE", "OMIE.CASH", "450"})

_NUM = r"\d{1,3}(?:\.\d{3})*,\d{2}"

# Variante A: <situação> <DD/MM> <descrição> <entradas|-> <saídas|-> <saldo>
_LINHA_DUAS_COLUNAS = re.compile(
    rf"^(\S+)\s+(\d{{2}}/\d{{2}})\s+(.*?)\s+(-|{_NUM})\s+(-|{_NUM})\s+({_NUM})\s*$"
)

# Variante B: <situação> <DD/MM> <descrição> <valor> <saldo>
_LINHA_UMA_COLUNA = re.compile(
    rf"^(\S+)\s+(\d{{2}}/\d{{2}})\s+(.*?)\s+(-?{_NUM})\s+(-?{_NUM})\s*$"
)

_SALDO_ANTERIOR = re.compile(rf"^(\d{{2}}/\d{{2}})\s+SALDO ANTERIOR\s+({_NUM})\s*$", re.I)
_PERIODO = re.compile(r"Per[ií]odo de\s+\d{2}/\d{2}/(\d{4})", re.IGNORECASE)

# O que identifica o Omie é o par "Situação ... Cliente ou Fornecedor" no
# cabeçalho — nenhum banco desta base nomeia colunas assim. As duas variantes
# se separam depois, pela presença de "Entradas".
_ASSINATURA = re.compile(
    r"\bsitua[çc][ãa]o\b.*\bdata\b.*\bcliente ou fornecedor\b.*\bsaldo\b",
    re.IGNORECASE,
)
_TEM_DUAS_COLUNAS = re.compile(r"\bentradas\b.*\bsa[íi]das\b", re.IGNORECASE)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def _duas_colunas(linhas: list[str]) -> bool:
    """Variante A quando o cabeçalho separa Entradas de Saídas."""
    for linha in linhas:
        if _ASSINATURA.search(linha):
            return bool(_TEM_DUAS_COLUNAS.search(linha))
    return True


def _ano_do_periodo(linhas: list[str], referencia_ano: int) -> int:
    for linha in linhas:
        achado = _PERIODO.search(linha)
        if achado:
            return int(achado.group(1))
    return referencia_ano


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    from datetime import date

    ano = _ano_do_periodo(linhas, referencia_ano)
    duas_colunas = _duas_colunas(linhas)
    padrao = _LINHA_DUAS_COLUNAS if duas_colunas else _LINHA_UMA_COLUNA
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    def data_de(texto: str) -> date | None:
        dia, mes = texto.split("/")
        try:
            return date(ano, int(mes), int(dia))
        except ValueError:
            return None

    for bruta in linhas:
        linha = bruta.strip()
        if not linha:
            continue

        abertura = _SALDO_ANTERIOR.match(linha)
        if abertura:
            saldo_anterior = parse_valor(abertura.group(2))
            continue

        casada = padrao.match(linha)
        if not casada:
            continue

        if duas_colunas:
            _situacao, data_str, descricao, entradas, saidas, saldo_str = casada.groups()
            if entradas != "-":
                valor = parse_valor(entradas)
            elif saidas != "-":
                bruto = parse_valor(saidas)
                valor = -abs(bruto) if bruto is not None else None
            else:
                continue
        else:
            _situacao, data_str, descricao, valor_str, saldo_str = casada.groups()
            valor = parse_valor(valor_str)

        data_lida = data_de(data_str)
        if data_lida is None:
            continue
        if valor is None or valor == 0:
            continue

        historico = re.sub(r"\s+", " ", descricao).strip() or "SEM DESCRIÇÃO"
        transacoes.append(
            TransacaoOFX(
                fitid=gerar_fitid(data_lida, historico, valor, idx),
                data=data_lida,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=parse_valor(saldo_str),
                ordem=idx,
            )
        )
        idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]
