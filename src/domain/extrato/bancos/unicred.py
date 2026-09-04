"""Extrato da Unicred (cooperativa de crédito).

    Coop: 582 - AG: 1740 - Conta: 182443
    Data Lançamentos Valor (R$) Saldo (R$)
    03/11/2025 PJ CONTA PJ 4 ( Doc.: 0 ) - R$ 129,50 -R$ 10.552,81
    DEBITO TRANSFERENCIA PIX ( Doc.: DEB PIX /
    03/11/2025 - R$ 100,00 -R$ 10.652,81
    CALHAS COLOMBO IND E COM LTDA )

O SINAL FICA ANTES DO `R$`, E A COLUNA DE SALDO TAMBÉM TEM SINAL

`- R$ 129,50` é débito; `R$ 1.200,00` é crédito. O saldo segue a mesma forma
(`-R$ 10.552,81`), e nesta conta ele passa o mês inteiro negativo — é conta com
cheque especial em uso. Um padrão que exigisse dígito depois do `R$` recusaria
o extrato inteiro.

Repare que o débito escreve `- R$` com espaço e o saldo escreve `-R$` sem. Os
dois precisam ser aceitos: é a mesma coluna do mesmo banco, com espaçamento
diferente conforme a largura do número.

A DESCRIÇÃO ENVOLVE A LINHA DE DADOS

Quando o texto é longo, a Unicred o quebra ACIMA e ABAIXO, e a linha de dados
fica sem descrição nenhuma — só data, valor e saldo:

    DEBITO TRANSFERENCIA PIX ( Doc.: DEB PIX /
    03/11/2025 - R$ 100,00 -R$ 10.652,81
    CALHAS COLOMBO IND E COM LTDA )

O parêntese que abre em cima fecha embaixo, o que confirma a ordem: o trecho
de cima vem primeiro. Sem colar os dois, o histórico fica vazio e o lançamento
chega à fila sem nada que identifique a contraparte.

`COOP:` NÃO BASTA PARA IDENTIFICAR

Viacredi e Sicredi também imprimem "Cooperativa". A assinatura aqui é a forma
abreviada com agência e conta na mesma linha (`Coop: 582 - AG: 1740 - Conta:`),
que nenhum dos outros usa.

NENHUM DOS SALDOS DO CABEÇALHO É USADO COMO ÂNCORA — E ISSO É DELIBERADO

O cabeçalho traz dois, e os dois enganam:

- `Saldo em 31/10/2025: R$ 10.423,31` **não carrega o sinal**. Medido em dois
  extratos com o mesmo formato: num deles a cadeia só fecha se o valor for
  POSITIVO, no outro só se for NEGATIVO. Não há no texto o que diferencie os
  dois casos, então usá-lo é escolher entre recusar um extrato bom e ancorar no
  valor errado — e ancorar errado desloca a conferência do mês inteiro.
- `Saldo atual` é o saldo do MOMENTO DA IMPRESSÃO, não do fim do período. Um
  extrato de junho emitido em julho já traz movimento de julho embutido nele.

O que sobra é a coluna `Saldo (R$)`, impressa em TODA linha e conferida contra
o lançamento anterior. Ela valida todos os passos menos o primeiro — e é
verdadeira, que é mais do que os dois do cabeçalho conseguem ser.
"""

from __future__ import annotations

import re
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"UNICRED", "136"})

_DATA = r"\d{2}/\d{2}/\d{4}"
# O sinal vem antes do "R$", com ou sem espaço entre os dois.
_VALOR = r"-?\s?R\$\s?\d{1,3}(?:\.\d{3})*,\d{2}"

_LINHA = re.compile(rf"^({_DATA})\s+(.*?)\s*({_VALOR})\s+({_VALOR})\s*$")

_ASSINATURA = re.compile(r"Coop:\s*\d+\s*-\s*AG:\s*\d+", re.IGNORECASE)

_IGNORAR = re.compile(
    r"^(Data\s+Lan[çc]amentos|Per[ií]odo de|Coop:|Solicitado por|Saldo atual|"
    r"Saldo em \d|Limite de cheque|Tarifas pendentes|Saldo bloq|"
    r"Lan[çc]amentos futuros|CENTRAL DE RELACIONAMENTO|\d{4} \d{3} \d{4}|"
    r"0800 |Extrato\s*$)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    return any(_ASSINATURA.search(linha) for linha in linhas)


def _eh_estrutural(linha: str) -> bool:
    limpa = linha.strip()
    return not limpa or bool(_LINHA.match(limpa) or _IGNORAR.match(limpa))


def _valor(texto: str) -> Decimal | None:
    """`- R$ 129,50` e `-R$ 10.552,81` chegam com o sinal separado do número."""
    limpo = texto.replace(" ", "")
    negativo = limpo.startswith("-")
    valor = parse_valor(limpo.lstrip("-"))
    if valor is None:
        return None
    return -valor if negativo else valor


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    limpas = [ln.strip() for ln in linhas]
    transacoes: list[TransacaoOFX] = []
    idx = 0

    for i, limpa in enumerate(limpas):
        if not limpa:
            continue

        casada = _LINHA.match(limpa)
        if not casada:
            continue

        data_str, meio, valor_str, saldo_str = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        valor = _valor(valor_str)
        saldo = _valor(saldo_str)
        if data_lida is None or valor is None or valor == 0:
            continue

        historico = re.sub(r"\s+", " ", meio).strip()
        if not historico:
            acima = limpas[i - 1] if i > 0 and not _eh_estrutural(limpas[i - 1]) else ""
            abaixo = (
                limpas[i + 1]
                if i + 1 < len(limpas) and not _eh_estrutural(limpas[i + 1])
                else ""
            )
            historico = " ".join(p for p in (acima, abaixo) if p).strip()
        historico = re.sub(r"\s+", " ", historico) or "SEM DESCRIÇÃO"

        transacoes.append(
            TransacaoOFX(
                fitid=gerar_fitid(data_lida, historico, valor, idx),
                data=data_lida,
                valor=valor,
                historico=historico[:200],
                tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                saldo_apos=saldo,
                ordem=idx,
            )
        )
        idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes)]
