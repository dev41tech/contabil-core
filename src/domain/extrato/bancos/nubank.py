"""Extrato do Nubank — o sinal vem da SEÇÃO em que a linha está, não do número.

O layout é hierárquico:

    Movimentações
    09 OUT 2025  Total de entradas   + 300,00
      Transferência Recebida Fulano - ***.440.409-** - NU        300,00
      PAGAMENTOS - IP (0260) Agência: 1 Conta:
      22578819-6
                 Total de saídas     - 250,00
      Aplicação RDB                                              250,00
                 Saldo do dia                                     50,00
    18 OUT 2025  Total de entradas   + 45.000,00
      ...

Os valores dos lançamentos **não têm sinal**. O que diz se é entrada ou saída é
a seção em que a linha aparece: tudo entre "Total de entradas" e "Total de
saídas" é crédito, e tudo entre "Total de saídas" e "Saldo do dia" é débito.
Lido linha a linha, sem essa noção de seção, todo lançamento vira crédito.

`Saldo do dia` fecha o dia e a lista é crescente, então ele pertence ao último
lançamento do dia — aqui sem a inversão que Stone, BBC, Cresol e Sicoob exigem.

Os mesmos rótulos "Total de entradas" e "Total de saídas" aparecem no resumo da
capa, antes de "Movimentações". Por isso a leitura só começa depois dele: na
capa esses rótulos são do período inteiro e não abrem seção nenhuma.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"NUBANK", "NU", "260"})

_NUM = r"\d{1,3}(?:\.\d{3})*,\d{2}"

_MESES = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

_INICIO = re.compile(r"^Movimenta[çc][õo]es\s*$", re.IGNORECASE)
_DIA = re.compile(r"^(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\b", re.IGNORECASE)
_ENTRADAS = re.compile(r"Total de entradas", re.IGNORECASE)
_SAIDAS = re.compile(r"Total de sa[íi]das", re.IGNORECASE)
_SALDO_DIA = re.compile(rf"^Saldo do dia\s+({_NUM})\s*$", re.IGNORECASE)
_LINHA = re.compile(rf"^(.*?)\s+({_NUM})\s*$")

_ASSINATURA = re.compile(r"^Saldo do dia\s+\d", re.IGNORECASE)
_SALDO_INICIAL = re.compile(rf"^Saldo inicial\s+({_NUM})\s*$", re.IGNORECASE)
_SALDO_FINAL = re.compile(rf"^Saldo final do per[ií]odo\s+({_NUM})\s*$", re.IGNORECASE)


def reconhece(linhas: list[str]) -> bool:
    tem_movimentacoes = any(_INICIO.match(linha.strip()) for linha in linhas)
    tem_saldo_do_dia = any(_ASSINATURA.match(linha.strip()) for linha in linhas)
    tem_dia = any(_DIA.match(linha.strip()) for linha in linhas)
    return tem_movimentacoes and tem_saldo_do_dia and tem_dia


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    # As duas pontas ficam na capa, ANTES de "Movimentações". Sem elas, um
    # extrato de um dia só tem uma única âncora e nada pode ser conferido — o
    # validador devolve "não conferida" e a importação cai na conferência por
    # totais, que o Nubank não imprime no formato esperado.
    saldo_inicial: Decimal | None = None
    saldo_final: Decimal | None = None
    for bruta in linhas:
        linha = bruta.strip()
        if saldo_inicial is None:
            achado = _SALDO_INICIAL.match(linha)
            if achado:
                saldo_inicial = parse_valor(achado.group(1))
        achado = _SALDO_FINAL.match(linha)
        if achado:
            saldo_final = parse_valor(achado.group(1))

    comecou = False
    sinal = 0
    dia_atual: date | None = None
    dias: list[tuple[date, Decimal | None, list[TransacaoOFX]]] = []
    idx = 0

    for bruta in linhas:
        linha = bruta.strip()
        if not linha:
            continue

        if not comecou:
            comecou = bool(_INICIO.match(linha))
            continue

        cabecalho = _DIA.match(linha)
        if cabecalho:
            dia, mes_txt, ano = cabecalho.groups()
            mes = _MESES.get(mes_txt.upper())
            if mes:
                try:
                    dia_atual = date(int(ano), mes, int(dia))
                except ValueError:
                    dia_atual = None
                if dia_atual is not None and (not dias or dias[-1][0] != dia_atual):
                    dias.append((dia_atual, None, []))
            # A própria linha do dia abre a seção de entradas.
            sinal = 1 if _ENTRADAS.search(linha) else sinal
            continue

        fechamento = _SALDO_DIA.match(linha)
        if fechamento and dias:
            data_do_dia, _antigo, lancamentos = dias[-1]
            dias[-1] = (data_do_dia, parse_valor(fechamento.group(1)), lancamentos)
            sinal = 0
            continue

        if _ENTRADAS.search(linha):
            sinal = 1
            continue
        if _SAIDAS.search(linha):
            sinal = -1
            continue

        if sinal == 0 or not dias:
            continue

        casada = _LINHA.match(linha)
        if not casada:
            # Continuação da descrição (agência, conta, documento).
            if dias[-1][2]:
                alvo = dias[-1][2][-1]
                dias[-1][2][-1] = replace(
                    alvo, historico=f"{alvo.historico} {linha}".strip()[:200]
                )
            continue

        descricao, valor_str = casada.groups()
        bruto = parse_valor(valor_str)
        if bruto is None or bruto == 0:
            continue
        valor = abs(bruto) * sinal
        historico = re.sub(r"\s+", " ", descricao).strip() or "SEM DESCRIÇÃO"
        dias[-1][2].append(
            TransacaoOFX(
                fitid=gerar_fitid(dias[-1][0], historico, valor, idx),
                data=dias[-1][0],
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=None,
                ordem=idx,
            )
        )
        idx += 1

    transacoes: list[TransacaoOFX] = []
    for _do_dia, saldo, lancamentos in dias:
        if not lancamentos:
            continue
        if saldo is not None:
            lancamentos[-1] = replace(lancamentos[-1], saldo_apos=saldo)
        transacoes.extend(lancamentos)

    if not transacoes:
        return []
    return [
        Bloco(
            transacoes=transacoes,
            saldo_anterior=saldo_inicial,
            saldo_final=saldo_final,
        )
    ]
