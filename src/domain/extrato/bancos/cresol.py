"""Extrato da Cresol — "Saldo do Dia" de fechamento, impresso antes dos lançamentos.

O layout é:

    31/12/2025 Saldo do Dia: + R$ 40.954,86
    COMPRAS NO DEBITO CARTAO
    31/12/2025                                                    - R$ 270,00
    MASTERCARD AGRO RURAL TOLEDO
    30/12/2025 Saldo do Dia: + R$ 41.224,86
    30/12/2025 PIX DEBITO PARA: ERALDO KISTEMACHER              - R$ 400,00

Duas coisas, as duas já vistas em outros bancos desta base:

- **Ordem decrescente com o saldo de fechamento no topo do dia**, como no BBC:
  31/12 fecha em 40.954,86, e 41.224,86 (fechamento de 30/12) − 270,00 dá
  exatamente isso. O saldo do cabeçalho pertence ao ÚLTIMO lançamento do dia em
  ordem cronológica.
- **A descrição ora está na linha, ora em volta dela**, como no Bradesco. Quem
  separa os dois casos é a própria linha de dados: se entre a data e o valor há
  texto, o lançamento se descreve sozinho; se não há, a descrição são as linhas
  de texto puro imediatamente acima e abaixo.

O sinal vem separado do valor (`- R$ 270,00`, `+ R$ 17.009,99`).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"CRESOL", "133"})

_MOEDA = r"[+-]\s?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"
_DATA = r"\d{2}/\d{2}/\d{4}"

_CABECALHO_DIA = re.compile(
    rf"^({_DATA})\s+Saldo do Dia:\s*({_MOEDA})\s*$", re.IGNORECASE
)
_LINHA = re.compile(rf"^({_DATA})\s+(.*?)\s*({_MOEDA})\s*$")

# A assinatura exige a DATA COMPLETA antes do rótulo. "Saldo do dia:" sozinho
# não identifica a Cresol — o Inter também o imprime, e como ele tem dias com
# saldo negativo, até o sinal antes do "R$" casava.
_ASSINATURA = re.compile(
    r"^\d{2}/\d{2}/\d{4}\s+Saldo do Dia:", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(Ag[eê]ncia\s|Saldo em Conta|Lan[çc]amentos\s*$|Consulta Posi[çc][ãa]o|"
    r"R\$\s|\d{2} de \w+ de \d{4} a |Per[ií]odo de |P[áa]gina \d)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    limpas = [ln.strip() for ln in linhas]

    # Um grupo por dia, na ordem impressa (do mais recente ao mais antigo).
    dias: list[tuple[date, Decimal | None, list[TransacaoOFX]]] = []
    idx = 0

    def eh_dados(i: int) -> bool:
        return (
            0 <= i < len(limpas)
            and bool(limpas[i])
            and (_LINHA.match(limpas[i]) is not None
                 or _CABECALHO_DIA.match(limpas[i]) is not None)
        )

    for i, linha in enumerate(limpas):
        if not linha or _IGNORAR.match(linha):
            continue

        cabecalho = _CABECALHO_DIA.match(linha)
        if cabecalho:
            do_dia = parse_data(cabecalho.group(1), referencia_ano)
            if do_dia is None:
                continue
            # Dia que atravessa a quebra de página tem o cabeçalho REPETIDO no
            # topo da página seguinte, com o mesmo saldo de fechamento. Abrir um
            # grupo novo ali ancoraria o mesmo saldo duas vezes e quebraria a
            # cadeia por um dia inteiro de movimento.
            if dias and dias[-1][0] == do_dia:
                continue
            dias.append(
                (do_dia, parse_valor(cabecalho.group(2).replace(" ", "")), [])
            )
            continue

        casada = _LINHA.match(linha)
        if not casada or not dias:
            continue

        data_str, meio, valor_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        if data_lida is None:
            continue
        valor = parse_valor(valor_str.replace(" ", ""))
        if valor is None or valor == 0:
            continue

        meio = meio.strip()
        if meio:
            historico = meio
        else:
            acima = limpas[i - 1] if i > 0 and not eh_dados(i - 1) else ""
            abaixo = limpas[i + 1] if not eh_dados(i + 1) else ""
            if _IGNORAR.match(acima or "x"):
                acima = ""
            if _IGNORAR.match(abaixo or "x"):
                abaixo = ""
            historico = " ".join(p for p in (acima, abaixo) if p).strip()

        historico = re.sub(r"\s+", " ", historico) or "SEM DESCRIÇÃO"
        dias[-1][2].append(
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
    for _data_do_dia, saldo, lancamentos in reversed(dias):
        if not lancamentos:
            continue
        cronologicos = list(reversed(lancamentos))
        if saldo is not None:
            cronologicos[-1] = replace(cronologicos[-1], saldo_apos=saldo)
        transacoes.extend(cronologicos)

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes)]

