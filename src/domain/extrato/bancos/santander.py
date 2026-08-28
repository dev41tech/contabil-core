"""Extrato do Santander Empresas — decrescente, com a descrição quebrada em volta.

O layout é:

    Data        Histórico                          Documento    Valor (R$)   Saldo (R$)
    24/06/2026  Tarifa Avulsa Envio Pix            23/06/2026        -9,90        31,54
    24/06/2026  Tarifa Restritivo                  16/06/2026       -19,00        41,44
                Tarifa Mensalidade Pacote Servicos
    23/06/2026                                                      -237,00   245.745,36
                MAIO / 2026

Três coisas:

- **Ordem decrescente**, como Stone, BBC, Cresol e Sicoob. A cadeia de saldos só
  fecha lendo de baixo para cima, e `ordem` sairia invertida na tela.
- **A descrição ora está na linha, ora em volta dela**, e a regra que separa os
  dois casos é a mesma do Bradesco: se entre a data e o valor há texto, o
  lançamento se descreve sozinho; se não há, a descrição são as linhas de texto
  puro imediatamente acima e abaixo.
- **Não há `SALDO ANTERIOR`.** O extrato abre direto nos lançamentos, então o
  primeiro em ordem cronológica não tem contra o que ser conferido — todos os
  outros têm, porque o Santander imprime saldo em toda linha.

O que NÃO é lançamento: o extrato termina com um quadro de composição de saldo
("A – Saldo de Conta Corrente", "B – Saldo Bloqueado", …). Nenhuma dessas linhas
começa com data e todas têm um número só, então a exigência de data no início
mais dois números no fim já as descarta — não é preciso listá-las uma a uma.

A assinatura exige **"Histórico"**: o cabeçalho do Sicredi é quase idêntico
(`Data Descrição Documento Valor (R$) Saldo (R$)`) e só essa palavra os separa.
"""

from __future__ import annotations

import re

from src.domain.extrato._comum import (
    Bloco,
    gerar_fitid,
    ordenar_do_mais_antigo,
    parse_data,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"SANTANDER", "033"})

_NUM = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
_DATA = r"\d{2}/\d{2}/\d{4}"

# O trecho da descrição é OPCIONAL: quando ela quebra em volta, a linha de dados
# fica só com data, valor e saldo (`11/06/2026 -3,41 -172,47`). Exigir texto no
# meio descartava justamente esses — e eram 9 dos 20 lançamentos do extrato
# medido, todos com a cadeia de saldos acusando o buraco.
_LINHA = re.compile(rf"^({_DATA})\s+(?:(.*?)\s+)?({_NUM})\s+({_NUM})\s*$")

_ASSINATURA = re.compile(
    r"\bdata\b.*\bhist[óo]rico\b.*\bdocumento\b.*\bvalor\b.*\bsaldo\b",
    re.IGNORECASE,
)

_IGNORAR = re.compile(
    r"^(Santander|Per[ií]odos?:|Saldo dispon[íi]vel|Data\s+Hist)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    limpas = [ln.strip() for ln in linhas]
    transacoes: list[TransacaoOFX] = []
    idx = 0

    def eh_estrutural(i: int) -> bool:
        if not (0 <= i < len(limpas)) or not limpas[i]:
            return True
        return bool(_LINHA.match(limpas[i]) or _IGNORAR.match(limpas[i]))

    for i, linha in enumerate(limpas):
        if not linha or _IGNORAR.match(linha):
            continue
        casada = _LINHA.match(linha)
        if not casada:
            continue

        data_str, meio, valor_str, saldo_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        valor = parse_valor(valor_str)
        if data_lida is None or valor is None or valor == 0:
            continue

        meio = (meio or "").strip()
        if meio:
            historico = meio
        else:
            acima = limpas[i - 1] if i > 0 and not eh_estrutural(i - 1) else ""
            abaixo = limpas[i + 1] if not eh_estrutural(i + 1) else ""
            historico = " ".join(p for p in (acima, abaixo) if p)

        historico = re.sub(r"\s+", " ", historico).strip() or "SEM DESCRIÇÃO"
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
    return [Bloco(transacoes=ordenar_do_mais_antigo(transacoes))]
