"""Extrato do BBC — cabeçalho de dia com o saldo de FECHAMENTO daquele dia.

O layout é:

    Saldo inicial do período:  Total de entradas:  Total de saídas:  Saldo final do período:
    R$ 25.289,75               +R$ 81.777,00       -R$ 82.994,04     R$ 24.072,71

    Movimentações
    12 AGO 2026  Saldo do dia: R$ 24.072,71
    Transferência entre contas enviada                       -R$ 330,60
    11 AGO 2026  Saldo do dia: R$ 24.403,31
    Transferência entre contas enviada                        -R$ 51,60
    Transferência Pix recebida de EXEMPLO LTDA            +R$ 20.000,00

A armadilha está na combinação de duas coisas:

- **A ordem é decrescente**, dia a dia e dentro do dia.
- **O "Saldo do dia" é o de FECHAMENTO**, e vem impresso ANTES dos lançamentos
  daquele dia.

Ou seja: o saldo do cabeçalho de 12/08 pertence ao ÚLTIMO lançamento de 12/08 em
ordem cronológica, não ao primeiro que aparece embaixo dele. Ancorar no lugar
errado desloca a cadeia por um dia inteiro de movimento. Confere no arquivo
real: 11/08 fecha em 24.403,31, o único lançamento de 12/08 é −330,60, e
24.403,31 − 330,60 = 24.072,71, que é o "Saldo do dia" de 12/08 — e também o
"Saldo final do período" da capa.

O extrato traz as duas pontas (`Saldo inicial do período` e `Saldo final do
período`), então a cadeia é conferida do começo ao fim.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"BBC", "BBCDIGITAL"})

_MOEDA = r"[+-]?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"

_MESES = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

# 12 AGO 2026 Saldo do dia: R$ 24.072,71
_CABECALHO_DIA = re.compile(
    rf"^(\d{{1,2}})\s+([A-Z]{{3}})\s+(\d{{4}})\s+Saldo do dia:\s*({_MOEDA})\s*$",
    re.IGNORECASE,
)

# <descrição> <valor>
_LINHA = re.compile(rf"^(.*?)\s+({_MOEDA})\s*$")

_SALDO_INICIAL = re.compile(r"Saldo inicial do per[ií]odo", re.IGNORECASE)
_SALDO_FINAL = re.compile(r"Saldo final do per[ií]odo", re.IGNORECASE)
_SO_MOEDAS = re.compile(rf"^(?:{_MOEDA})(?:\s+(?:{_MOEDA}))*$")

_ASSINATURA = re.compile(r"Saldo do dia:\s*R\$", re.IGNORECASE)


def reconhece(linhas: list[str]) -> bool:
    tem_dia = any(_CABECALHO_DIA.match(linha.strip()) for linha in linhas)
    return tem_dia and any(_ASSINATURA.search(linha) for linha in linhas)


def _pontas(linhas: list[str]) -> tuple[Decimal | None, Decimal | None]:
    """Lê o saldo inicial e o final da capa.

    Os rótulos vêm numa linha e os valores na seguinte, lado a lado e na mesma
    ordem — é assim que o PDF diagrama as quatro caixas do resumo.
    """
    for i, bruta in enumerate(linhas):
        linha = bruta.strip()
        if not (_SALDO_INICIAL.search(linha) and _SALDO_FINAL.search(linha)):
            continue
        for seguinte in linhas[i + 1 : i + 3]:
            valores = re.findall(_MOEDA, seguinte.strip())
            if len(valores) == 4 and _SO_MOEDAS.match(seguinte.strip()):
                return parse_valor(valores[0]), parse_valor(valores[3])
    return None, None


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    inicial, final = _pontas(linhas)

    # Um grupo por dia, na ordem em que aparecem (do mais recente ao mais antigo).
    dias: list[tuple[date, Decimal | None, list[TransacaoOFX]]] = []
    idx = 0

    for bruta in linhas:
        linha = bruta.strip()
        if not linha:
            continue

        cabecalho = _CABECALHO_DIA.match(linha)
        if cabecalho:
            dia, mes_txt, ano, saldo_txt = cabecalho.groups()
            mes = _MESES.get(mes_txt.upper())
            if not mes:
                continue
            try:
                data_lida = date(int(ano), mes, int(dia))
            except ValueError:
                continue
            dias.append((data_lida, parse_valor(saldo_txt), []))
            continue

        if not dias:
            # Ainda na capa: os valores do resumo não são lançamentos.
            continue
        if _SO_MOEDAS.match(linha):
            continue

        casada = _LINHA.match(linha)
        if not casada:
            continue
        descricao, valor_str = casada.groups()
        valor = parse_valor(valor_str)
        if valor is None or valor == 0:
            continue
        historico = re.sub(r"\s+", " ", descricao).strip()
        if not historico:
            continue

        data_lida, _saldo, lancamentos = dias[-1]
        lancamentos.append(
            TransacaoOFX(
                fitid=gerar_fitid(data_lida, historico, valor, idx),
                data=data_lida,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=None,
                ordem=idx,
            )
        )
        idx += 1

    transacoes: list[TransacaoOFX] = []
    # Do dia mais antigo para o mais recente, e dentro do dia idem. O saldo do
    # cabeçalho é o de fechamento, então vai no ÚLTIMO lançamento do dia.
    for _data_do_dia, saldo, lancamentos in reversed(dias):
        if not lancamentos:
            continue
        cronologicos = list(reversed(lancamentos))
        if saldo is not None:
            cronologicos[-1] = _com_saldo(cronologicos[-1], saldo)
        transacoes.extend(cronologicos)

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=inicial, saldo_final=final)]


def _com_saldo(transacao: TransacaoOFX, saldo: Decimal) -> TransacaoOFX:
    from dataclasses import replace

    return replace(transacao, saldo_apos=saldo)
