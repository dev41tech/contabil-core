"""Extrato do Banco do Brasil — dois layouts, os dois conferíveis.

**Layout A — "Dt. balancete"**, uma linha por lançamento e coluna de saldo:

    Dt. balancete  Dt. movimento  Ag. origem  Lote  Histórico  Documento  Valor R$  Saldo
    30/01/2026     0000  00000000  Saldo Anterior                          8.334,85 D
    02/02/2026     0000  14397821  Pix - Recebido  21.538.488.153.761        350,00 C
                   02/02 15:38 14152014000135 Fulano Hage
    02/02/2026     0000  13113124  Débito Serviço Cobrança  1.806.030.017.027  0,99 D

**Layout B — "Dia Lote"**, cada lançamento partido em TRÊS alturas:

    Dia         Lote    Documento        Histórico              Valor
    31/12/2025
                        Saldo Anterior                          13,44 (-)
    02/01/2026                           Cobrança de I.O.F.
                13601   391100702                                0,29 (-)
                                         IOF Saldo Devedor Conta
    00/00/0000
                13113                    Saldo do dia           13,73 (-)

O que muda entre eles:

- **O sinal.** No A é a letra `D`/`C` à direita do número; no B é `(-)`/`(+)`.
- **A âncora de saldo.** O A tem coluna própria; o B não tem coluna nenhuma e
  publica o saldo em linhas `Saldo do dia`, cuja data vem como `00/00/0000`.
  Essa data inválida é do MARCADOR, não de um lançamento — e foi ela que me fez
  descartar o BB por engano numa primeira leitura, concluindo que não havia
  âncora utilizável. Havia: conferido na amostra AAJ, `Saldo Anterior 13,44 (-)`
  menos IOF de `0,29` dá exatamente o `Saldo do dia 13,73 (-)` seguinte.
- **A altura.** No A o lançamento é uma linha e o complemento vem abaixo, ambos
  a ~11,8 pontos — agrupar por altura juntaria o complemento com o lançamento
  SEGUINTE, então ali vale a altura padrão e o complemento é colado à parte. No
  B as três alturas de um lançamento ficam a ~5,3 e o grupo seguinte a ~20, o
  que permite juntá-las com altura de 14.

Nos dois, `Saldo Anterior` abre a cadeia. No B, cada `Saldo do dia` vira âncora
do último lançamento lido — é o que faz a conferência por segmentos fechar num
extrato que não imprime saldo por linha.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.domain.extrato._comum import (
    Bloco,
    agrupar_linhas,
    borda_direita,
    gerar_fitid,
    parse_data,
    parse_valor,
)
from src.domain.extrato.ofx_parser import TransacaoOFX

SIGLAS = frozenset({"BB", "BANCO DO BRASIL", "001"})

_VALOR = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_SINAL_PARENTESES = re.compile(r"^\(([+-])\)$")

_TOLERANCIA_COLUNA = 16.0
_DISTANCIA_DA_LETRA = 26.0
# No layout B as três alturas de um lançamento ficam a ~5,3 e o grupo seguinte
# a ~20 — 14 junta as três sem alcançar o de baixo.
_ALTURA_DIA_LOTE = 14.0

_ASSINATURA_BALANCETE = re.compile(
    r"\bdt\.?\s*balancete\b.*\bhist[óo]rico\b.*\bvalor\b", re.IGNORECASE
)
_ASSINATURA_DIA_LOTE = re.compile(
    r"^\s*Dia\s+Lote\s+Documento\s+Hist[óo]rico\s+Valor\s*$", re.IGNORECASE
)


def reconhece(linhas: list[str]) -> bool:
    return any(
        _ASSINATURA_BALANCETE.search(linha) or _ASSINATURA_DIA_LOTE.match(linha.strip())
        for linha in linhas
    )


def _sinal_a_direita(x1: float, marcas: list[tuple[float, int]]) -> int | None:
    """O marcador de sinal mais próximo à direita do número."""
    candidatas = [
        (x0 - x1, sinal) for x0, sinal in marcas if 0 <= x0 - x1 <= _DISTANCIA_DA_LETRA
    ]
    return min(candidatas)[1] if candidatas else None


# `2.259,65 C4.242,55 D` — quando o valor e o saldo ficam colados, o pdfplumber
# devolve a letra do sinal grudada no número seguinte. O token resultante não é
# número nem letra, então o saldo se perdia e o sinal do valor ficava sem dono.
_LETRA_COLADA = re.compile(r"^([DC])(\d{1,3}(?:\.\d{3})*,\d{2})$")
# Largura aproximada da letra, para devolver ao número a borda direita que ele
# já tinha — é ela que identifica a coluna.
_LARGURA_DA_LETRA = 6.0


def _separar_letra_colada(palavras: list[dict]) -> list[dict]:
    """Desgruda a letra do sinal do número que veio atrás dela."""
    resultado: list[dict] = []
    for palavra in palavras:
        casada = _LETRA_COLADA.match(palavra["text"])
        if not casada:
            resultado.append(palavra)
            continue
        x0, x1 = float(palavra["x0"]), float(palavra["x1"])
        resultado.append(
            {"text": casada.group(1), "x0": x0, "x1": x0 + _LARGURA_DA_LETRA,
             "top": palavra["top"]}
        )
        resultado.append(
            {"text": casada.group(2), "x0": x0 + _LARGURA_DA_LETRA, "x1": x1,
             "top": palavra["top"]}
        )
    return resultado


# ─────────────────────────────────────────────────────── layout A: balancete


def _colunas_balancete(linhas: list[list[dict]]) -> dict | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if "balancete" not in textos or "valor" not in textos or "saldo" not in textos:
            continue
        if not any(t.startswith("hist") for t in textos):
            continue
        valor = borda_direita(textos, palavras, "valor")
        saldo = borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue
        return {
            "x_data": float(palavras[textos.index("balancete")]["x0"]) - 15,
            "x_historico": float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("hist"))]["x0"]
            ),
            "valor": valor,
            "saldo": saldo,
        }
    return None


def _extrair_balancete(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0
    colunas: dict | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(_separar_letra_colada(palavras))
        colunas = _colunas_balancete(linhas) or colunas
        if colunas is None:
            continue

        for linha in linhas:
            if _colunas_balancete([linha]) is not None:
                continue

            data_lida: date | None = None
            descricao: list[str] = []
            numeros: list[tuple[float, str, Decimal]] = []
            marcas: list[tuple[float, int]] = []

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA.match(texto) and abs(x0 - colunas["x_data"]) <= 14:
                    data_lida = parse_data(texto, referencia_ano)
                    continue
                if texto.upper() in ("D", "C") and len(texto) == 1:
                    marcas.append((x0, -1 if texto.upper() == "D" else 1))
                    continue
                if _VALOR.match(texto):
                    for nome in ("valor", "saldo"):
                        if abs(x1 - colunas[nome]) <= _TOLERANCIA_COLUNA:
                            convertido = parse_valor(texto)
                            if convertido is not None:
                                numeros.append((x1, nome, convertido))
                            break
                    else:
                        if x0 >= colunas["x_historico"] - 3:
                            descricao.append(texto)
                    continue
                if x0 >= colunas["x_historico"] - 3:
                    descricao.append(texto)

            texto_descricao = re.sub(r"\s+", " ", " ".join(descricao)).strip()

            valor = saldo = None
            for x1, nome, bruto in numeros:
                sinal = _sinal_a_direita(x1, marcas)
                if sinal is None:
                    continue
                if nome == "valor":
                    valor = abs(bruto) * sinal
                else:
                    saldo = abs(bruto) * sinal

            if texto_descricao.upper().startswith("SALDO ANTERIOR"):
                # O valor da abertura sai na coluna de saldo.
                if saldo_anterior is None:
                    saldo_anterior = saldo if saldo is not None else valor
                continue

            if valor is None or valor == 0:
                # Linha de complemento: pertence ao lançamento de cima.
                if texto_descricao and transacoes:
                    alvo = transacoes[-1]
                    transacoes[-1] = replace(
                        alvo,
                        historico=f"{alvo.historico} {texto_descricao}".strip()[:200],
                    )
                continue
            if data_lida is None:
                continue

            historico = texto_descricao[:200] or "SEM DESCRIÇÃO"
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, historico, valor, idx),
                    data=data_lida,
                    valor=valor,
                    historico=historico,
                    tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                    saldo_apos=saldo,
                    ordem=idx,
                )
            )
            idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]


# ──────────────────────────────────────────────────────── layout B: dia/lote


def _colunas_dia_lote(linhas: list[list[dict]]) -> dict | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if textos[:5] != ["dia", "lote", "documento", "histórico", "valor"] and \
           textos[:5] != ["dia", "lote", "documento", "historico", "valor"]:
            continue
        return {
            "x_data": float(palavras[0]["x0"]),
            "x_historico": float(palavras[3]["x0"]),
            "valor": float(palavras[4]["x1"]) + 10,
        }
    return None


def _extrair_dia_lote(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0
    colunas: dict | None = None

    for palavras in paginas:
        palavras = _separar_letra_colada(palavras)
        # O cabeçalho é lido na altura PADRÃO: com 14 pontos ele se funde com a
        # linha de baixo e deixa de ser reconhecível.
        estreitas = agrupar_linhas(palavras)
        colunas = _colunas_dia_lote(estreitas) or colunas
        if colunas is None:
            continue

        # E as palavras dele saem da página antes do agrupamento largo. O
        # cabeçalho se repete no topo de cada página, e ali ele fica a menos de
        # 14 pontos da primeira linha de dados: fundido, ele parte o lançamento
        # em dois grupos e o valor se perde. Foi assim que sumiu um pagamento de
        # boleto de R$ 190,00 — a cadeia de saldos acusou a diferença exata.
        do_cabecalho = {
            (round(float(w["top"]), 2), round(float(w["x0"]), 2))
            for linha in estreitas
            if _colunas_dia_lote([linha]) is not None
            for w in linha
        }
        uteis = [
            w
            for w in palavras
            if (round(float(w["top"]), 2), round(float(w["x0"]), 2)) not in do_cabecalho
        ]
        linhas = agrupar_linhas(uteis, altura=_ALTURA_DIA_LOTE)

        for linha in linhas:

            data_lida: date | None = None
            descricao: list[str] = []
            numeros: list[tuple[float, Decimal]] = []
            marcas: list[tuple[float, int]] = []

            for palavra in linha:
                texto = palavra["text"]
                x0, x1 = float(palavra["x0"]), float(palavra["x1"])

                if _DATA.match(texto) and abs(x0 - colunas["x_data"]) <= 10:
                    # `00/00/0000` marca a linha de saldo; não é data de nada.
                    if texto != "00/00/0000":
                        data_lida = parse_data(texto, referencia_ano)
                    continue
                sinal = _SINAL_PARENTESES.match(texto)
                if sinal:
                    marcas.append((x0, 1 if sinal.group(1) == "+" else -1))
                    continue
                if _VALOR.match(texto) and abs(x1 - colunas["valor"]) <= 24:
                    convertido = parse_valor(texto)
                    if convertido is not None:
                        numeros.append((x1, convertido))
                    continue
                if x0 >= colunas["x_historico"] - 3:
                    descricao.append(texto)

            texto_descricao = re.sub(r"\s+", " ", " ".join(descricao)).strip()
            maiuscula = texto_descricao.upper()

            valor = None
            for x1, bruto in numeros:
                sinal_lido = _sinal_a_direita(x1, marcas)
                if sinal_lido is not None:
                    valor = abs(bruto) * sinal_lido

            if maiuscula.startswith("SALDO ANTERIOR"):
                if saldo_anterior is None:
                    saldo_anterior = valor
                continue

            if maiuscula.startswith("SALDO DO DIA"):
                # Âncora: é o saldo DEPOIS do último lançamento lido.
                if valor is not None and transacoes:
                    transacoes[-1] = replace(transacoes[-1], saldo_apos=valor)
                continue

            if valor is None or valor == 0 or data_lida is None:
                continue

            historico = texto_descricao[:200] or "SEM DESCRIÇÃO"
            transacoes.append(
                TransacaoOFX(
                    fitid=gerar_fitid(data_lida, historico, valor, idx),
                    data=data_lida,
                    valor=valor,
                    historico=historico,
                    tipo_ofx="CREDIT" if valor > 0 else "DEBIT",
                    saldo_apos=None,
                    ordem=idx,
                )
            )
            idx += 1

    if not transacoes:
        return []
    return [Bloco(transacoes=transacoes, saldo_anterior=saldo_anterior)]


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    """Escolhe o layout pelo cabeçalho da tabela."""
    for palavras in paginas:
        estreitas = agrupar_linhas(palavras)
        if _colunas_balancete(estreitas) is not None:
            return _extrair_balancete(paginas, referencia_ano)
        if _colunas_dia_lote(estreitas) is not None:
            return _extrair_dia_lote(paginas, referencia_ano)
    return []
