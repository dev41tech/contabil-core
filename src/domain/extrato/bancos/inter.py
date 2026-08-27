"""Extrato do Banco Inter — a data é cabeçalho de seção, não da linha.

O layout é:

    2 de Janeiro de 2026 Saldo do dia: R$ 1.575,54
    Pix enviado: "Cp :60701190-Everth Ruan Lopes Ribeiro" -R$ 40,00 R$ 710,83
    Pix recebido: "Cp :60746948-GRISOPAR.COM TRANSPORTE C. E." R$ 2.000,00 R$ 2.127,89
    3 de Janeiro de 2026 Saldo do dia: R$ 328,14
    ...

Duas coisas que nenhum outro banco desta base faz:

1. **A data é um cabeçalho de dia, escrita por extenso**, e vale para todas as
   linhas abaixo dela até o próximo cabeçalho. A linha do lançamento não tem
   data nenhuma — o parser genérico exige data na linha (ou herdada de uma linha
   anterior que a tivesse) e por isso devolvia zero.
2. **Os valores vêm com `R$`** e o sinal antes do símbolo (`-R$ 40,00`).

O "Saldo do dia" do cabeçalho é o saldo de FECHAMENTO daquele dia, não o de
abertura — não serve de `saldo_anterior` do bloco e por isso não é usado como
tal. A conferência aqui é a cadeia de saldos, que o Inter imprime em toda linha.
"""

from __future__ import annotations

import re
from datetime import date

from src.domain.extrato._comum import (
    Bloco,
    gerar_fitid,
    parse_data_extenso,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"INTER", "077"})

_MOEDA = r"-?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"

# <descrição> <valor> <saldo>
_LINHA = re.compile(rf"^(.*?)\s+({_MOEDA})\s+({_MOEDA})\s*$")

_CABECALHO_DIA = re.compile(r"^\d{1,2}\s+de\s+[A-Za-zçÇãÃéÉ]+\s+de\s+\d{4}\b")

_IGNORAR = re.compile(
    r"^(Solicitado em:|CPF/CNPJ:|Per[ií]odo:|Saldo total|\(bloqueado|"
    r"Fale com a gente|SAC:|Ouvidoria|Defici[eê]ncia)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    for linha in linhas[:40]:
        if "banco inter" in linha.lower():
            return True
        if _CABECALHO_DIA.match(linha.strip()) and "saldo do dia" in linha.lower():
            return True
    return False


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    idx = 0
    dia: date | None = None

    for raw in linhas:
        linha = raw.strip()
        if not linha or _IGNORAR.match(linha):
            continue

        if _CABECALHO_DIA.match(linha):
            d = parse_data_extenso(linha)
            if d:
                dia = d
            continue

        m = _LINHA.match(linha)
        if not m or dia is None:
            continue

        descricao, valor_str, saldo_str = m.groups()
        valor = parse_valor(valor_str)
        if valor is None or valor == 0:
            continue

        historico = re.sub(r"\s+", " ", descricao.strip()) or "SEM DESCRIÇÃO"

        transacoes.append(
            TransacaoOFX(
                fitid=gerar_fitid(dia, historico, valor, idx),
                data=dia,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor >= 0 else "DEBIT",
                saldo_apos=parse_valor(saldo_str),
                ordem=idx,
            )
        )
        idx += 1

    return [Bloco(transacoes=transacoes)] if transacoes else []
