"""Extrato Detalhado do Banco Daycoval.

    Data Nº Docto Lançamento Débito (R$) Crédito (R$) Saldo (R$)
    SALDO ANTERIOR 73,23
    02/07/2026 9235551 TARIFA DE MANUTENCAO DE C/C 43,79 -
    SALDO EM 02/07/2026 29,44
    RECEBIMENTO PIX - Cp: 90400888-3689-130363264-SHPX
    09/07/2026 8292562 - 589.145,98
    LOGISTICA LTDA

O SINAL VEM DE QUAL COLUNA RECEBEU O NÚMERO

Débito e crédito são colunas separadas, e a que não recebeu o lançamento é
impressa como um traço. Então a linha sempre termina em dois campos, e
exatamente um deles é valor:

    ... 43,79 -     → o número está na coluna de DÉBITO  → negativo
    ... - 589.145,98 → o número está na coluna de CRÉDITO → positivo

Não dá para decidir pelo texto: o mesmo `TARIFA` aparece nos dois lados em
outros extratos, e `AMORT. DE CONTRATO` aqui é débito enquanto `TED-CREDITO` é
crédito. Quem decide é a posição, e ela é legível sem coordenada porque o traço
ocupa o lugar da coluna vazia.

A DESCRIÇÃO ÀS VEZES NÃO CABE NA LINHA

Quando o texto é longo, o Daycoval o quebra em volta da linha de dados — parte
ACIMA, parte ABAIXO — e a coluna `Lançamento` da própria linha fica vazia:

    RECEBIMENTO PIX - Cp: 90400888-3689-130363264-SHPX
    09/07/2026 8292562 - 589.145,98
    LOGISTICA LTDA

É a mesma forma já tratada em Cresol e Itaú. A ordem de leitura importa: o
trecho de cima vem antes, senão o histórico sai com o fim na frente do começo.

O TRAÇO NO MEIO DA DESCRIÇÃO NÃO CONFUNDE

`TED-CREDITO - 341 7285 989245 X ONE EXPRESS LTDA - 21.572,55` tem um traço
solto dentro do texto. O casamento é ancorado no FIM da linha e exige que um
dos dois últimos campos seja valor monetário com centavos — `341` não é, então
o traço do meio nunca é lido como coluna vazia.

O SALDO DO DIA É ÂNCORA, NÃO LANÇAMENTO

`SALDO EM DD/MM/AAAA` fecha o dia e pertence ao ÚLTIMO lançamento dele. Pode
ser negativo (`SALDO EM 06/07/2026 -17,44`), como em qualquer conta com limite.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from src.domain.extrato._comum import Bloco, gerar_fitid, parse_data, parse_valor
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"DAYCOVAL", "707"})

_DATA = r"\d{2}/\d{2}/\d{4}"
_VALOR = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
# Uma das duas colunas de valor: o número, ou o traço que marca a coluna vazia.
_CAMPO = rf"(?:{_VALOR}|-)"

_LINHA = re.compile(
    rf"^({_DATA})\s+(\S+)\s*(.*?)\s+({_CAMPO})\s+({_CAMPO})\s*$"
)
_SALDO_DIA = re.compile(rf"^SALDO\s+EM\s+({_DATA})\s+({_VALOR})\s*$", re.IGNORECASE)
_SALDO_ANTERIOR = re.compile(rf"^SALDO\s+ANTERIOR\s+({_VALOR})\s*$", re.IGNORECASE)

_ASSINATURA = re.compile(
    r"Extrato Detalhado|Dayconnect|DAYCOVAL", re.IGNORECASE
)

_IGNORAR = re.compile(
    r"^(Data\s+N[ºo]\s+Docto|P[áa]gina\s+\d|Titular\s*$|Ag[êe]ncia\s*$|Conta\s*$|"
    r"Per[ií]odo consultado|Impress[ãa]o realizada|Central de Atendimento|"
    r"SAC\s|Ouvidoria|Atendimento de segunda|Limite\(|Saldo Bloqueado|"
    r"Valor Bloqueado|Saldo Dispon[ií]vel|- Os saldos acima|lan[çc]amentos\.)",
    re.IGNORECASE,
)


def reconhece(linhas: list[str]) -> bool:
    tem_marca = any(_ASSINATURA.search(linha) for linha in linhas)
    tem_saldo_dia = any(_SALDO_DIA.match(linha.strip()) for linha in linhas)
    return tem_marca and tem_saldo_dia


def _eh_estrutural(linha: str) -> bool:
    """Linha que não é texto solto de descrição."""
    limpa = linha.strip()
    if not limpa:
        return True
    return bool(
        _LINHA.match(limpa)
        or _SALDO_DIA.match(limpa)
        or _SALDO_ANTERIOR.match(limpa)
        or _IGNORAR.match(limpa)
    )


def extrair(linhas: list[str], referencia_ano: int) -> list[Bloco]:
    limpas = [ln.strip() for ln in linhas]
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0

    for i, limpa in enumerate(limpas):
        if not limpa:
            continue

        if saldo_anterior is None:
            abertura = _SALDO_ANTERIOR.match(limpa)
            if abertura:
                saldo_anterior = parse_valor(abertura.group(1))
                continue

        fecho = _SALDO_DIA.match(limpa)
        if fecho:
            # O saldo do dia pertence ao último lançamento já lido dele.
            do_dia = parse_data(fecho.group(1), referencia_ano)
            saldo = parse_valor(fecho.group(2))
            if transacoes and saldo is not None and transacoes[-1].data == do_dia:
                transacoes[-1] = replace(transacoes[-1], saldo_apos=saldo)
            continue

        if _IGNORAR.match(limpa):
            continue

        casada = _LINHA.match(limpa)
        if not casada:
            continue

        data_str, documento, meio, campo_debito, campo_credito = casada.groups()
        data_lida = parse_data(data_str, referencia_ano)
        if data_lida is None:
            continue

        if campo_debito != "-" and campo_credito == "-":
            valor = parse_valor(campo_debito)
            valor = -abs(valor) if valor is not None else None
        elif campo_debito == "-" and campo_credito != "-":
            valor = parse_valor(campo_credito)
            valor = abs(valor) if valor is not None else None
        else:
            # Os dois traços, ou os dois com número: a linha não é um lançamento
            # que este layout saiba ler, e adivinhar o lado seria inventar sinal.
            continue
        if valor is None or valor == 0:
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
        historico = re.sub(r"\s+", " ", historico) or f"DOC {documento}"

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
