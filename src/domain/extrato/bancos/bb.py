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


# Quando o cabeçalho vem empilhado, `Dt. balancete` se parte em duas alturas e
# nenhuma linha contém a assinatura inteira. O que sobra é a linha só com os dois
# rótulos de baixo — que, sozinha, poderia ser de qualquer documento contábil.
# Por isso ela vale como assinatura apenas ACOMPANHADA da linha de colunas.
_ASSINATURA_EMPILHADA = re.compile(r"^\s*balancete\s+movimento\s*$", re.IGNORECASE)
# Painel PJ: os dois rótulos viram colunas próprias, na ordem inversa, e o
# `No.DOCUMENTO` sem espaço é marca da tela do BB.
_ASSINATURA_PAINEL = re.compile(
    r"\bmovimento\b.*\bbalancete\b.*\bhist[óo]rico\b.*\bvalor\b.*\bsaldo\b",
    re.IGNORECASE,
)
_COLUNAS_DO_EXTRATO = re.compile(
    r"\bhist[óo]rico\b.*\bvalor\b.*\bsaldo\b", re.IGNORECASE
)


def reconhece(linhas: list[str]) -> bool:
    if any(
        _ASSINATURA_BALANCETE.search(linha)
        or _ASSINATURA_DIA_LOTE.match(linha.strip())
        or _ASSINATURA_PAINEL.search(linha)
        for linha in linhas
    ):
        return True
    return any(_ASSINATURA_EMPILHADA.match(linha) for linha in linhas) and any(
        _COLUNAS_DO_EXTRATO.search(linha) for linha in linhas
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
# Um valor como `12.376,74` ocupa ~30 pontos; 40 cobre a coluna inteira com
# folga e ainda fica muito à direita da coluna de descrição.
_LARGURA_DA_COLUNA_NUMERICA = 40.0


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


# O cabeçalho do balancete nem sempre cabe numa altura só. Na exportação
# "Consultas - Extrato de conta corrente" ele vem empilhado em TRÊS:
#
#     Dt.                                                        ← 199,1
#     Ag. origem  Lote  Histórico  Documento  Valor R$  Saldo    ← 203,6
#     balancete   movimento                                      ← 207,6
#
# `balancete` fica numa linha e `Valor`/`Saldo` em outra, a 4 pontos. Exigir os
# quatro na MESMA linha fazia o adaptador ser escolhido pela sigla da agência,
# reconhecer o banco e extrair zero lançamentos — recusa sem explicação.
_ALTURA_DO_CABECALHO = 10.0


def _colunas_balancete(linhas: list[list[dict]]) -> dict | None:
    for indice, palavras in enumerate(linhas):
        textos = [p["text"].lower() for p in palavras]
        if "valor" not in textos or "saldo" not in textos:
            continue
        if not any(t.startswith("hist") for t in textos):
            continue
        valor = borda_direita(textos, palavras, "valor")
        saldo = borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue

        # `balancete` ancora a coluna de data. Ele pode estar nesta linha ou na
        # de cima/de baixo, quando o cabeçalho vem empilhado.
        balancete = _palavra_vizinha(linhas, indice, "balancete")
        if balancete is None:
            continue

        return {
            "x_data": float(balancete["x0"]) - 15,
            "x_historico": float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("hist"))]["x0"]
            ),
            "valor": valor,
            "saldo": saldo,
        }
    return None


# O BB numera o histórico, e o número cai DENTRO da coluna de descrição:
#
#     0000  13128  500  BB GIRO PRONAMPE  300.712.236.000.795  1.350,95 D
#     0000  00000  000  Saldo Anterior                         1.031,50 D
#                  ↑ código do histórico, não descrição
#
# Além de sujar o histórico, ele quebrava a abertura da cadeia: a descrição
# saía "000 Saldo Anterior" e o teste de `SALDO ANTERIOR` no início do texto
# não casava, deixando o extrato sem saldo de abertura.
_CODIGO_DO_HISTORICO = re.compile(r"^\d{1,4}\s+(?=\D)")


def _e_linha_de_fecho(texto: str) -> str:
    """`S A L D O`, com as letras soltas, é a linha de fecho do extrato."""
    return texto.replace(" ", "").upper() == "SALDO"


def _limpar_codigo_do_historico(texto: str) -> str:
    return _CODIGO_DO_HISTORICO.sub("", texto, count=1)


def _palavra_vizinha(linhas: list[list[dict]], indice: int, alvo: str) -> dict | None:
    """Procura uma palavra na linha dada e nas que estão à altura do cabeçalho."""
    referencia = float(linhas[indice][0]["top"])
    for outras in linhas:
        if abs(float(outras[0]["top"]) - referencia) > _ALTURA_DO_CABECALHO:
            continue
        for palavra in outras:
            if palavra["text"].lower() == alvo:
                return palavra
    return None


def _extrair_balancete(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    saldo_final: Decimal | None = None
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
                # A letra do sinal mora à direita das colunas numéricas. Sem
                # essa condição, QUALQUER `D` ou `C` solto da linha virava
                # marcador — e a linha de fecho `S A L D O`, que traz as letras
                # separadas, perdia o próprio D e deixava de ser reconhecida.
                if (
                    texto.upper() in ("D", "C")
                    and len(texto) == 1
                    and x0 >= colunas["valor"] - _LARGURA_DA_COLUNA_NUMERICA
                ):
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

            texto_descricao = _limpar_codigo_do_historico(
                re.sub(r"\s+", " ", " ".join(descricao)).strip()
            )

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

            # O fecho do extrato vem com as letras SEPARADAS — `S A L D O` —,
            # numa linha de código de histórico 999. Sem reconhecê-la, ela virava
            # complemento do último lançamento e o bloco ficava sem saldo final:
            #
            #     30/04/2026  0000 00000 123  Cobrança de Juros   6,74 D
            #     30/04/2026  0000 00000 999  S A L D O                  0,80 C
            #
            # A cauda depois da última âncora fica então inconferível, e um mês
            # inteiro é recusado por causa da linha que justamente o fecharia.
            if _e_linha_de_fecho(texto_descricao):
                if saldo is not None:
                    saldo_final = saldo
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
    return [
        Bloco(
            transacoes=transacoes,
            saldo_anterior=saldo_anterior,
            saldo_final=saldo_final,
        )
    ]


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


# ──────────────────────────────────────────────────────── layout C: painel PJ
#
# Impressão da tela do Painel PJ, não do extrato. Vem com o menu do site ao
# redor da tabela, mas a tabela em si é a mais limpa dos três layouts:
#
#     MOVIMENTO   BALANCETE  HISTÓRICO           No.DOCUMENTO  VALOR      SALDO
#     31/12/2025             Saldo Anterior                              -R$ 245,30
#     02/01/2026             Cobrança de I.O.F.  391100702     -R$ 4,87  -R$ 250,17
#     07/01/2026             Pix - Recebido      71514122485021 R$ 3.000,00
#
# O que muda para os outros dois: o sinal é o `-` colado no `R$` ANTES do
# número, não uma letra depois dele; e o saldo sai só na última linha do dia,
# o que a conferência por segmentos já sabe tratar.

_MARCA_REAIS = re.compile(r"^(-?)R\$$")
# Distância entre o `R$` e o número que ele qualifica: medido, fica em 2 e 3
# pontos. 12 dá folga sem alcançar a coluna vizinha, que está a dezenas.
_DISTANCIA_DO_CIFRAO = 12.0


def _colunas_painel(linhas: list[list[dict]]) -> dict | None:
    for palavras in linhas:
        textos = [p["text"].lower() for p in palavras]
        if not {"movimento", "balancete", "valor", "saldo"} <= set(textos):
            continue
        if not any(t.startswith("hist") for t in textos):
            continue
        # O cabeçalho do balancete, quando cabe numa linha só, também tem os
        # dois rótulos — e casaria aqui, ancorando a data na coluna errada.
        # Duas marcas separam os layouts, e as duas vêm do mesmo fato: no
        # balancete os rótulos são `Dt. balancete` e `Dt. movimento`, nessa
        # ordem; no Painel são colunas próprias, sem `Dt.` e na ordem inversa.
        if "dt." in textos:
            continue
        if textos.index("movimento") > textos.index("balancete"):
            continue
        valor = borda_direita(textos, palavras, "valor")
        saldo = borda_direita(textos, palavras, "saldo")
        if valor is None or saldo is None:
            continue
        return {
            "x_data": float(palavras[textos.index("movimento")]["x0"]),
            "x_historico": float(
                palavras[next(i for i, t in enumerate(textos) if t.startswith("hist"))]["x0"]
            ),
            "valor": valor,
            "saldo": saldo,
        }
    return None


def _sinal_a_esquerda(x0: float, marcas: list[tuple[float, int]]) -> int | None:
    """O `R$` mais próximo à esquerda do número."""
    candidatas = [
        (x0 - x1, sinal) for x1, sinal in marcas if 0 <= x0 - x1 <= _DISTANCIA_DO_CIFRAO
    ]
    return min(candidatas)[1] if candidatas else None


def _extrair_painel(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    transacoes: list[TransacaoOFX] = []
    saldo_anterior: Decimal | None = None
    idx = 0
    colunas: dict | None = None

    for palavras in paginas:
        linhas = agrupar_linhas(palavras)
        colunas = _colunas_painel(linhas) or colunas
        if colunas is None:
            continue

        for linha in linhas:
            if _colunas_painel([linha]) is not None:
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
                marca = _MARCA_REAIS.match(texto)
                if marca:
                    marcas.append((x1, -1 if marca.group(1) else 1))
                    continue
                if _VALOR.match(texto):
                    for nome in ("valor", "saldo"):
                        if abs(x1 - colunas[nome]) <= _TOLERANCIA_COLUNA:
                            convertido = parse_valor(texto)
                            if convertido is not None:
                                numeros.append((x0, nome, convertido))
                            break
                    else:
                        if x0 >= colunas["x_historico"] - 3:
                            descricao.append(texto)
                    continue
                if x0 >= colunas["x_historico"] - 3:
                    descricao.append(texto)

            texto_descricao = re.sub(r"\s+", " ", " ".join(descricao)).strip()

            valor = saldo = None
            for x0, nome, bruto in numeros:
                sinal = _sinal_a_esquerda(x0, marcas)
                if sinal is None:
                    continue
                if nome == "valor":
                    valor = abs(bruto) * sinal
                else:
                    saldo = abs(bruto) * sinal

            if texto_descricao.upper().startswith("SALDO ANTERIOR"):
                if saldo_anterior is None:
                    saldo_anterior = saldo if saldo is not None else valor
                continue

            if valor is None or valor == 0:
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


def extrair_de_palavras(paginas: list[list[dict]], referencia_ano: int) -> list[Bloco]:
    """Escolhe o layout pelo cabeçalho da tabela.

    O Painel é testado ANTES do balancete e a ordem não é estilo: o cabeçalho
    dele — `MOVIMENTO BALANCETE HISTÓRICO No.DOCUMENTO VALOR SALDO` — satisfaz
    as condições do balancete, que então ancorava a data em `BALANCETE` (x=269)
    quando ela mora sob `MOVIMENTO` (x=183). Nenhuma data casava com a coluna,
    e o layout extraía zero lançamentos sem errar em nada visível.
    """
    for palavras in paginas:
        estreitas = agrupar_linhas(palavras)
        if _colunas_painel(estreitas) is not None:
            return _extrair_painel(paginas, referencia_ano)
        if _colunas_balancete(estreitas) is not None:
            return _extrair_balancete(paginas, referencia_ano)
        if _colunas_dia_lote(estreitas) is not None:
            return _extrair_dia_lote(paginas, referencia_ano)
    return []
