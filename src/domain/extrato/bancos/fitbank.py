"""Extrato do Omie.CASH / FitBank — duas colunas de valor, com "-" na que está vazia.

O layout é:

    Situação   Data   Cliente ou Fornecedor   Categoria        Entradas  Saídas  Saldo
               31/05  SALDO ANTERIOR                                             19,40
    Conciliado 01/06  QUEBRA PRECO            Clientes - ...   3.570,00  -       3.589,40
    Conciliado 01/06  tar.20260601.163923     Tarifas Banc.    -         1,99    3.587,41

O que ele tem de próprio:

- **A coluna vazia vem escrita como `-`**, não em branco. É isso que diz o sinal:
  valor em Entradas é crédito, em Saídas é débito. Como as duas colunas sempre
  aparecem (uma delas como `-`), a leitura é posicional e não depende de
  coordenada.
- **A data não tem ano** (`01/06`). O ano sai do cabeçalho "Período de
  01/06/2026 até 30/06/2026" — usar o ano corrente erraria todo extrato de
  dezembro processado em janeiro.
- Traz `SALDO ANTERIOR` e saldo em toda linha, então a cadeia fecha inteira.
"""

from __future__ import annotations

import re
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"FITBANK", "OMIE", "OMIE.CASH", "450"})

_NUM = r"\d{1,3}(?:\.\d{3})*,\d{2}"

# <situação> <DD/MM> <descrição> <entradas|-> <saídas|-> <saldo>
_LINHA = re.compile(
    rf"^(\S+)\s+(\d{{2}}/\d{{2}})\s+(.*?)\s+(-|{_NUM})\s+(-|{_NUM})\s+({_NUM})\s*$"
)

_SALDO_ANTERIOR = re.compile(rf"^(\d{{2}}/\d{{2}})\s+SALDO ANTERIOR\s+({_NUM})\s*$", re.I)
_PERIODO = re.compile(r"Per[ií]odo de\s+\d{2}/\d{2}/(\d{4})", re.IGNORECASE)

_ASSINATURA = re.compile(
    r"\bsitua[çc][ãa]o\b.*\bdata\b.*\bentradas\b.*\bsa[íi]das\b.*\bsaldo\b",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def _ano_do_periodo(linhas: list[str], referencia_ano: int) -> int:
    for linha in linhas:
        achado = _PERIODO.search(linha)
        if achado:
            return int(achado.group(1))
    return referencia_ano


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    from datetime import date

    ano = _ano_do_periodo(linhas, referencia_ano)
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

        casada = _LINHA.match(linha)
        if not casada:
            continue

        _situacao, data_str, descricao, entradas, saidas, saldo_str = casada.groups()
        data_lida = data_de(data_str)
        if data_lida is None:
            continue

        if entradas != "-":
            valor = parse_valor(entradas)
        elif saidas != "-":
            bruto = parse_valor(saidas)
            valor = -abs(bruto) if bruto is not None else None
        else:
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
