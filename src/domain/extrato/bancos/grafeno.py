"""Extrato da Grafeno — data e hora em linhas diferentes, do mais recente ao mais antigo.

O layout é:

    DATA / HORA  LANÇAMENTO  NOME · DOC · BANCO / AG / CONTA   VALOR (R$)  SALDO (R$)
    31/07/2026   SALDO FINAL                                                R$ 555,68
    31/07/2026   Tarifas de conta                              -R$250,00    R$ 555,68
    04:08
    16/07/2026   PIX Enviado  POTENCIAL SERVICOS ... LTDA      -R$200.000,00 R$ 813,16
    10:44        06.915.323/0001-91 · Bco 237 · Ag 5755 · Cc 00252250-0
    04:32        Recebimento de boletos                        +R$200.000,00 R$ 200.813,16
    01/07/2026   SALDO INICIAL                                               R$ 827,09

Diferente dos outros, este é legível por linha — a coluna é única e a extração
não embaralha nada. O que ele tem de próprio:

- **A hora fica na linha de baixo**, e uma linha que começa por hora pode ser
  duas coisas: a continuação da anterior (traz o CNPJ e o banco da contraparte,
  que é justamente o que a conciliação usa) ou um lançamento inteiro, quando o
  dia já foi impresso mais acima. O que separa é ter valor.
- **A ordem é decrescente**, como na Stone.
- **`SALDO INICIAL` e `SALDO FINAL`** vêm impressos, então as duas pontas da
  cadeia são conferidas — inclusive o primeiro lançamento e a cauda.
- O sinal vem colado no valor (`-R$250,00`, `+R$200.000,00`).
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import (
    Bloco,
    gerar_fitid,
    ordenar_do_mais_antigo,
    parse_data,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"GRAFENO", "384"})

_MOEDA = r"[+-]?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"
_PREFIXO = r"(?:(\d{2}/\d{2}/\d{4})|(\d{2}:\d{2}))"

# <data|hora> <descrição> <valor> <saldo>
_LINHA = re.compile(rf"^{_PREFIXO}\s+(.*?)\s+({_MOEDA})\s+({_MOEDA})\s*$")

# <data> SALDO INICIAL|FINAL <saldo>   — um valor só
_LINHA_SALDO = re.compile(
    rf"^{_PREFIXO}\s+SALDO\s+(INICIAL|FINAL)\s+({_MOEDA})\s*$", re.IGNORECASE
)

# <hora> <texto>   — continuação da linha de cima (CNPJ, banco, agência)
_CONTINUACAO = re.compile(r"^(\d{2}:\d{2})\s*(.*)$")

_ASSINATURA = re.compile(
    r"\bdata\s*/\s*hora\b.*\blan[çc]amento\b.*\bvalor\b.*\bsaldo\b", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(Extrato\s+Detalhado|Gerado em:|CNPJ:|Per[ií]odo:|Grafeno\s)", re.IGNORECASE
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_inicial: Decimal | None = None
    saldo_final: Decimal | None = None
    idx = 0
    ultima_data: date | None = None

    for bruta in linhas:
        linha = bruta.strip()
        if not linha or _IGNORAR.match(linha) or _ASSINATURA.search(linha):
            continue

        marco = _LINHA_SALDO.match(linha)
        if marco:
            data_str, _, qual, valor_str = marco.groups()
            if data_str:
                lida = parse_data(data_str, referencia_ano)
                if lida:
                    ultima_data = lida
            saldo = parse_valor(valor_str)
            if qual.upper() == "INICIAL":
                saldo_inicial = saldo
            else:
                saldo_final = saldo
            continue

        casada = _LINHA.match(linha)
        if casada:
            data_str, _hora, descricao, valor_str, saldo_str = casada.groups()
            if data_str:
                lida = parse_data(data_str, referencia_ano)
                if lida:
                    ultima_data = lida
            if ultima_data is None:
                continue
            valor = parse_valor(valor_str)
            if valor is None or valor == 0:
                continue
            historico = re.sub(r"\s+", " ", descricao).strip() or "SEM DESCRIÇÃO"
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(ultima_data, historico, valor, idx),
                    data=ultima_data,
                    valor=valor,
                    historico=historico[:200],
                    tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                    saldo_apos=parse_valor(saldo_str),
                    ordem=idx,
                )
            )
            idx += 1
            continue

        # Linha que começa por hora e não tem valor: é a continuação da de cima,
        # e traz o CNPJ e o banco da contraparte. Vale colar — é o que permite
        # resolver a contraparte por documento em vez de por nome.
        continuacao = _CONTINUACAO.match(linha)
        if continuacao and transacoes:
            complemento = continuacao.group(2).strip()
            if complemento:
                alvo = transacoes[-1]
                juntado = f"{alvo.historico} {complemento}".strip()[:200]
                transacoes[-1] = replace(alvo, historico=juntado)

    if not transacoes:
        return []
    return [
        Bloco(
            transacoes=ordenar_do_mais_antigo(transacoes),
            saldo_anterior=saldo_inicial,
            saldo_final=saldo_final,
        )
    ]
