"""Extrato de Movimentação do Banco Safra.

    Data Lançamento Complemento Nº Documento Valor (R$)
    29/07 RESGATE FUNDO INVEST SAFRA SOBERANO MAX 269062182 100.000,00
    29/07 PIX ENVIADO COUTINHO INCORPORADORA DE 268724779 -100.000,00
    BENS 46515935000101
    29/07 SALDO TOTAL 1.986,41
    03/08 OUTROS CUSTOS BMF 271490805 -0,80

A DATA NÃO TRAZ O ANO

`29/07`, não `29/07/2026`. O ano vem de fora, do `referencia_ano` que o parser
já resolve pelo período impresso no cabeçalho — mesma situação do Itaú. Não é
detalhe: um extrato de dezembro lido em janeiro cairia no ano errado inteiro, e
o erro é invisível porque a data continua sendo uma data válida.

`SALDO TOTAL` E `SALDO INICIAL` SÃO ÂNCORA, NÃO LANÇAMENTO

As duas linhas têm a forma exata de um lançamento — data, texto, valor — e
somá-las duplicaria o saldo dentro do movimento. O que as separa é só o texto,
então ele é verificado antes de a linha virar transação.

`SALDO TOTAL` fecha o dia e pertence ao ÚLTIMO lançamento dele.

A DESCRIÇÃO TRANSBORDA PARA A LINHA DE BAIXO

`PIX ENVIADO COUTINHO INCORPORADORA DE` continua em `BENS 46515935000101`. Aqui
a quebra é só para baixo — diferente do Daycoval, que quebra para os dois lados.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"SAFRA", "422"})

_DATA = r"\d{2}/\d{2}"
_VALOR = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"

_LINHA = re.compile(rf"^({_DATA})\s+(.+?)\s+({_VALOR})\s*$")

# Rótulos que ocupam a coluna de descrição mas não são movimento.
_SALDO_DO_DIA = re.compile(r"^SALDO\s+TOTAL$", re.IGNORECASE)
_SALDO_ABERTURA = re.compile(r"^SALDO\s+INICIAL$", re.IGNORECASE)

_ASSINATURA = re.compile(
    r"Banco Safra S/?A|Extrato de Movimenta[çc][ãa]o", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(Data\s+Lan[çc]amento|LAN[ÇC]AMENTOS REALIZADOS|CNPJ:|Per[ií]odo de|"
    r"Saldo \+ Limite|R\$\s|CENTRAL DE SUPORTE|\(\d{2}\)\s|\d{4} \d{3} \d{4}|"
    r"personalizado|a 6[ªa] feira|\d{2}h, exceto|P[áa]gina \d)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def _eh_estrutural(linha: str) -> bool:
    limpa = linha.strip()
    return not limpa or bool(_LINHA.match(limpa) or _IGNORAR.match(limpa))


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    limpas = [ln.strip() for ln in linhas]
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    for i, limpa in enumerate(limpas):
        if not limpa or _IGNORAR.match(limpa):
            continue

        casada = _LINHA.match(limpa)
        if not casada:
            continue

        data_str, meio, valor_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        valor = parse_valor(valor_str)
        if data_lida is None or valor is None:
            continue

        rotulo = re.sub(r"\s+", " ", meio).strip()

        if _SALDO_DO_DIA.match(rotulo):
            if transacoes and transacoes[-1].data == data_lida:
                transacoes[-1] = replace(transacoes[-1], saldo_apos=valor)
            continue

        if _SALDO_ABERTURA.match(rotulo):
            # Só vale como abertura do extrato se nada foi lido ainda; no meio
            # do arquivo é a abertura de um dia, e a cadeia já a cobre.
            if not transacoes and saldo_anterior is None:
                saldo_anterior = valor
            continue

        if valor == 0:
            continue

        historico = rotulo
        # A descrição transborda para a linha de baixo quando não cabe.
        if i + 1 < len(limpas) and not _eh_estrutural(limpas[i + 1]):
            historico = f"{historico} {limpas[i + 1]}".strip()
        historico = re.sub(r"\s+", " ", historico) or "SEM DESCRIÇÃO"

        transacoes.append(
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

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]
